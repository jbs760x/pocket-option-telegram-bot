# Po247 bot – PO-first signals with optional fallback
# - /pologin <ssid> (stores session token in memory)
# - /postatus       (shows UID, saved SSID tail, scan interval, min payout, fallback flag)
# - /scan <secs>    (set how often autosignal would check; manual mode by default)
# - /check <SYMBOL> [tf=1min]  (EURUSD-OTC, GBPUSD-OTC, ...; returns direction + confidence + payout)
# - /payout <SYMBOL>           (shows current PO payout if available)
# - /fallback on|off           (allow Twelve/Alpha fallback if PO blocks)
# - /potest [SYMBOL] [tf]      (quick PO connectivity test)
# - /result win|loss, /stats, /reset (simple session stats)
#
# Notes
# - PO HTTP goes through optional proxy: set PO_PROXY_URL env like https://USER:PASS@host:port
# - If PO blocks, turn fallback on: /fallback on
# - All commands are MANUAL (no auto trading). You act on the signals.
#
# Fill BOT_TOKEN and ADMIN_ID below.

import os, re, json, math, time, asyncio, logging, aiohttp
from datetime import datetime, timezone
from collections import deque
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ====== YOUR TELEGRAM CREDENTIALS ======
BOT_TOKEN = "8471181182:AAFEhPc59AvzNsnuPbj-N2PatGbvgZnnd_0"
ADMIN_ID  = 7814662315  # your Telegram user id

# ====== DATA PROVIDERS / ENV ======
PO_BASE = os.getenv("PO_BASE", "https://po.market").rstrip("/")
PO_PROXY_URL = os.getenv("PO_PROXY_URL", "").strip()  # eg https://user:pass@proxy:port
# (Keep these as-is unless you know PO endpoints changed in your region)
PO_CANDLES_PATH = os.getenv("PO_CANDLES_PATH", "/api/v1/candles")
PO_PAYOUT_PATH  = os.getenv("PO_PAYOUT_PATH",  "/api/v1/payout")

# fallback keys (optional; you already have these)
TWELVE_KEY = "9aa4ea677d00474aa0c3223d0c812425"
ALPHA_KEY  = "BM22MZEIOLL68RI6"

# ====== DEFAULTS / STATE ======
SCAN_INTERVAL = 120       # seconds (manual mode—this just controls ETA messaging)
MIN_PAYOUT     = 0.70     # % threshold shown in status/help
ALLOW_FALLBACK = False    # PO first by default

STATS = {"wins":0, "losses":0, "total":0}
LAST_FIRES: dict[str, deque] = {}
COOLDOWN_SEC = 240
MAX_PER_HOUR = 6

# pocket option session (ssid = sessionToken)
STATE = {
    "ssid": "",
    "uid":  "93269888",   # you shared this
    "saved": None,
}

# ========= Utils =========
def _dir_to_arrow(direction: str) -> str:
    return "UP" if direction.lower() == "call" else "DOWN"

def ema(values, period):
    if len(values) < period: return None
    k = 2/(period+1)
    ev = sum(values[:period]) / period
    for v in values[period:]:
        ev = v*k + ev*(1-k)
    return ev

def rsi(values, period=14):
    if len(values) <= period: return None
    gains, losses = [], []
    for i in range(1,len(values)):
        ch = values[i]-values[i-1]
        gains.append(max(ch,0)); losses.append(max(-ch,0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period,len(values)-1):
        avg_g = (avg_g*(period-1) + gains[i]) / period
        avg_l = (avg_l*(period-1) + losses[i]) / period
    if avg_l == 0: return 100.0
    rs = avg_g/avg_l
    return 100 - (100/(1+rs))

def norm_symbol(sym: str) -> str:
    return sym.upper().replace(" ", "")

def tf_ok(tf: str) -> bool:
    return tf in {"1min","5min","15min","30min","45min","1h","2h","4h","8h","1day","1week","1month"}

# ========= AIOHTTP helpers (proxy-aware) =========
def _po_headers():
    c = STATE["ssid"].strip()
    # send both cookie names; PO variants accept one of them
    ck = f"ssid={c}; sessionToken={c}" if c else ""
    hdrs = {"User-Agent":"Mozilla/5.0 (bot)","Accept":"application/json"}
    if ck: hdrs["Cookie"] = ck
    return hdrs

def _session():
    # trust_env honors HTTPS_PROXY/HTTP_PROXY if you prefer those
    return aiohttp.ClientSession(trust_env=True)

async def po_fetch_json(url: str, headers=None, timeout=12):
    kw = {"timeout": aiohttp.ClientTimeout(total=timeout)}
    if PO_PROXY_URL:
        kw["proxy"] = PO_PROXY_URL
    async with _session() as s:
        async with s.get(url, headers=headers or {}, **kw) as r:
            if r.status != 200:
                text = await r.text()
                raise RuntimeError(f"PO HTTP {r.status}: {text[:180]}")
            ct = r.headers.get("content-type","")
            if "json" not in ct.lower():
                raise RuntimeError(f"PO bad content-type: {ct}")
            return await r.json()

# ========= Providers =========
async def po_get_candles(symbol: str, tf="1min", limit=120):
    """Try Pocket Option first; raise on error so caller can fallback."""
    if not tf_ok(tf): raise ValueError("bad tf")
    sym = norm_symbol(symbol)
    url = f"{PO_BASE}{PO_CANDLES_PATH}?asset={sym}&tf={tf}&limit={limit}"
    js = await po_fetch_json(url, headers=_po_headers(), timeout=12)
    # Map your PO JSON into [{"open":...,"high":...,"low":...,"close":...}]
    # Accept common shapes: {"data":[{o,h,l,c}...]} or direct list
    raw = js.get("data", js)
    candles=[]
    for c in raw[-limit:]:
        o = float(c.get("open", c.get("o", c.get("Open", 0))))
        h = float(c.get("high", c.get("h", c.get("High", o))))
        l = float(c.get("low",  c.get("l", c.get("Low",  o))))
        cl= float(c.get("close",c.get("c", c.get("Close", o))))
        candles.append({"open":o,"high":h,"low":l,"close":cl})
    if not candles: raise RuntimeError("PO returned no candles")
    return candles

async def td_get_candles(symbol: str, tf="1min", limit=120):
    if not TWELVE_KEY: raise RuntimeError("no Twelve key")
    s = norm_symbol(symbol)
    if "/" not in s and len(s)==6: s = f"{s[:3]}/{s[3:]}"
    url = (f"https://api.twelvedata.com/time_series?symbol={s}"
           f"&interval={tf}&outputsize={limit}&apikey={TWELVE_KEY}")
    async with _session() as ss:
        async with ss.get(url, timeout=12) as r:
            js = await r.json()
            vals = js.get("values")
            if not vals: raise RuntimeError(js.get("message","Twelve no data"))
            vals = list(reversed(vals))[-limit:]
            return [{"open":float(x["open"]), "high":float(x["high"]),
                     "low":float(x["low"]), "close":float(x["close"])} for x in vals]

async def po_get_payout(symbol: str):
    """Best-effort payout read from PO; return None if unavailable."""
    try:
        sym = norm_symbol(symbol)
        url = f"{PO_BASE}{PO_PAYOUT_PATH}?asset={sym}"
        js = await po_fetch_json(url, headers=_po_headers(), timeout=10)
        # accept {"payout": 0.82} or {"data":{"payout":82}}
        if "payout" in js: 
            v = js["payout"]
        else:
            v = js.get("data",{}).get("payout")
        if v is None: return None
        return float(v) if v<=1.0 else float(v)/100.0
    except Exception:
        return None

async def get_candles(symbol: str, tf="1min", limit=120):
    """PO first; fallback optional."""
    try:
        return await po_get_candles(symbol, tf, limit), "po"
    except Exception as e:
        if not ALLOW_FALLBACK:
            raise
        try:
            return await td_get_candles(symbol, tf, limit), "twelve"
        except Exception as e2:
            raise RuntimeError(f"PO fail & fallback fail: {e} | {e2}")

# ========= Scoring =========
def decide(closes):
    if len(closes) < 50: return None, 0.0
    e50 = ema(closes, 50); r = rsi(closes,14)
    if e50 is None or r is None: return None, 0.0
    last = closes[-1]
    # combo of trend + RSI
    if last > e50 and r < 40:
        prob = 0.60 + (40-r)/200  # 60–80%
        return "call", min(prob, 0.8)
    if last < e50 and r > 60:
        prob = 0.60 + (r-60)/200
        return "put", min(prob, 0.8)
    return None, 0.0

# ========= Commands =========
async def start_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Po24/7 (manual)\n"
        "/pologin <ssid>\n"
        "/postatus\n"
        "/check <SYMBOL> [tf=1min]\n"
        "/payout <SYMBOL>\n"
        "/scan <secs>\n"
        "/fallback on|off\n"
        "/potest [SYMBOL] [tf]\n"
        "/result win|loss | /stats | /reset\n"
        "Pairs like EURUSD-OTC, GBPUSD-OTC. Timeframes: 1min,5min,15min,..."
    )

async def pologin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args: 
        return await update.message.reply_text("Usage: /pologin <ssid>")
    STATE["ssid"] = ctx.args[0].strip()
    STATE["saved"] = datetime.now(timezone.utc).isoformat()
    await update.message.reply_text("🔐 SSID saved.")

async def postatus(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tail = (STATE["ssid"][:4]+"..."+STATE["ssid"][-4:]) if STATE["ssid"] else "-"
    await update.message.reply_text(
        f"UID: {STATE['uid']}\nSSID: {tail}\nSaved: {STATE['saved'] or '-'}\n"
        f"Scan interval: {SCAN_INTERVAL}s\nMin payout: {int(MIN_PAYOUT*100)}%\n"
        f"Fallback: {'ON' if ALLOW_FALLBACK else 'OFF'}"
    )

async def scan_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global SCAN_INTERVAL
    if not ctx.args:
        return await update.message.reply_text(f"Scan interval: {SCAN_INTERVAL}s")
    try:
        n = max(30, int(ctx.args[0]))
        SCAN_INTERVAL = n
        await update.message.reply_text(f"✅ Scan interval set: {SCAN_INTERVAL}s")
    except:
        await update.message.reply_text("Usage: /scan <seconds>")

async def fallback_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global ALLOW_FALLBACK
    if not ctx.args:
        return await update.message.reply_text(f"Fallback is {'ON' if ALLOW_FALLBACK else 'OFF'}")
    v = ctx.args[0].lower()
    if v in ("on","off"):
        ALLOW_FALLBACK = (v == "on")
        return await update.message.reply_text(f"✅ Fallback {v.upper()}")
    await update.message.reply_text("Usage: /fallback on|off")

async def potest_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sym = norm_symbol(ctx.args[0]) if ctx.args else "EURUSD-OTC"
    tf  = ctx.args[1] if len(ctx.args)>1 else "1min"
    try:
        url = f"{PO_BASE}{PO_CANDLES_PATH}?asset={sym}&tf={tf}&limit=1"
        _ = await po_fetch_json(url, headers=_po_headers(), timeout=8)
        await update.message.reply_text(f"PO OK for {sym} {tf}")
    except Exception as e:
        await update.message.reply_text(f"PO FAIL: {e}")

async def payout_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args: return await update.message.reply_text("Usage: /payout SYMBOL")
    sym = norm_symbol(ctx.args[0])
    p = await po_get_payout(sym)
    if p is None: 
        return await update.message.reply_text("Payout unavailable right now.")
    await update.message.reply_text(f"{sym} payout: {int(p*100)}%")

async def check_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args: 
        return await update.message.reply_text("Usage: /check SYMBOL [tf=1min]")
    sym = norm_symbol(ctx.args[0])
    tf  = (ctx.args[1] if len(ctx.args)>1 else "1min").lower()
    if not tf_ok(tf):
        return await update.message.reply_text("Bad tf. Try 1min,5min,15min,...")
    try:
        candles, src = await get_candles(sym, tf, 120)
        closes = [c["close"] for c in candles]
        decision, prob = decide(closes)
        if not decision:
            return await update.message.reply_text(
                f"📊 {sym} {tf} [{src}] – no strong edge right now."
            )
        payout = await po_get_payout(sym)
        payout_txt = f" | payout {int(payout*100)}%" if payout is not None else ""
        await update.message.reply_text(
            f"📣 {sym} {tf} [{src}]\n"
            f"Direction: {_dir_to_arrow(decision)}\n"
            f"Confidence: {int(prob*100)}%{payout_txt}\n"
            f"Next check ~{SCAN_INTERVAL}s"
        )
        STATS["total"] += 1
    except Exception as e:
        await update.message.reply_text(f"❌ Data error: {e}")

async def result_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args or ctx.args[0].lower() not in ("win","loss"):
        return await update.message.reply_text("Usage: /result win|loss")
    if ctx.args[0].lower()=="win": STATS["wins"]+=1
    else: STATS["losses"]+=1
    tot = max(1, STATS["wins"]+STATS["losses"])
    wr = STATS["wins"]/tot*100
    await update.message.reply_text(
        f"Recorded {ctx.args[0].upper()} – W:{STATS['wins']} L:{STATS['losses']} (WR {wr:.1f}%)"
    )

async def stats_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tot = STATS["wins"]+STATS["losses"]
    wr  = (STATS["wins"]/tot*100) if tot else 0.0
    await update.message.reply_text(
        f"Stats – Signals: {STATS['total']} | W:{STATS['wins']} L:{STATS['losses']} (WR {wr:.1f}%)"
    )

async def reset_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATS.update({"wins":0,"losses":0,"total":0})
    await update.message.reply_text("✅ Stats reset.")

# ========= main =========
async def _post_init(app: Application):
    # Make sure we use long polling (no webhook conflict)
    try:
        await app.bot.delete_webhook()
    except Exception:
        pass

def main():
    app = Application.builder().token(BOT_TOKEN).post_init(_post_init).build()
    app.add_handler(CommandHandler(["start","help"], start_help))
    app.add_handler(CommandHandler("pologin", pologin))
    app.add_handler(CommandHandler("postatus", postatus))
    app.add_handler(CommandHandler("scan", scan_cmd))
    app.add_handler(CommandHandler("fallback", fallback_cmd))
    app.add_handler(CommandHandler("potest", potest_cmd))
    app.add_handler(CommandHandler("payout", payout_cmd))
    app.add_handler(CommandHandler("check", check_cmd))
    app.add_handler(CommandHandler("result", result_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    # echo "SYMBOL call|put AMOUNT DURATION" (manual logging only)
    async def echo_parse(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not update.message or not update.message.text: return
        m = re.match(r"^\s*([A-Za-z0-9/_\-.]+)\s+(call|put)\s+(\d+(?:\.\d+)?)\s+(\d+)\s*$", update.message.text, re.I)
        if not m: return
        s,d,a,dur = m.groups()
        await update.message.reply_text(
            f"✅ Signal noted\nPair: {s.upper()}\nDirection: {_dir_to_arrow(d)}\n"
            f"Amount: ${a}\nDuration: {dur}s"
        )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo_parse))
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()