# PocketOption OTC Telegram Bot — Signals Only (Render-ready, command-triggered)
# - Uses TwelveData primary + AlphaVantage fallback for candles
# - /pologin persists & verifies your Pocket Option session (no auto-trading)
# - High-confidence signals: EMA50 + RSI14 + momentum + 15m trend confirm + ATR body filter
# - /autosignal (one pair) and /autosignalmulti (many pairs) — run only when you command
# - /multisignal one-shot scan of watchlist
# - Stop after 3 losses (no win cap)
# - python-telegram-bot v21 (no Updater issues)

import os, json, logging, aiohttp, asyncio, math
from datetime import datetime, timezone, timedelta
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ===== YOUR CREDENTIALS (already filled) =====
BOT_TOKEN  = "8471181182:AAFEhPc59AvzNsnuPbj-N2PatGbvgZnnd_0"
ADMIN_ID   = 7814662315

TWELVE_KEY = "9aa4ea677d00474aa0c3223d0c812425"
ALPHA_KEY  = "BM22MZEIOLL68RI6"

# Pocket Option session & uid (from you)
PO_UID = "93269888"
HARDCODED_SSID = "d7a8a43d4618a7227c6ed769f8fd9975"  # sessionToken you gave me

# ===== Persistent Pocket Option SSID storage =====
STATE_FILE = "po_state.json"
PO = {"ssid": HARDCODED_SSID or None, "verified": False, "saved_at": None, "uid": PO_UID}

def _load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    # prefer hardcoded SSID if present
                    if HARDCODED_SSID:
                        data["ssid"] = HARDCODED_SSID
                        data["verified"] = False
                    if "uid" not in data and PO_UID:
                        data["uid"] = PO_UID
                    PO.update(data)
                    logging.info("Loaded PO state.")
    except Exception as e:
        logging.warning(f"Load state failed: {e}")

def _save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(PO, f)
    except Exception as e:
        logging.warning(f"Save state failed: {e}")

_load_state()

# ===== STATS & SETTINGS =====
LOSS_LIMIT = 3                  # stop after 3 losses; NO win cap
STATE = {
    "wins": 0, "losses": 0, "total": 0, "daily_losses": 0,
    "auto": False, "task": None, "mode": "both",
    "multirun": False, "multitask": None
}
WATCHLIST = ["EURUSD-OTC","GBPUSD-OTC","USDJPY-OTC","AUDCAD-OTC"]  # you can change with /watch add/remove/list
DEFAULT_TF = "1min"
DEFAULT_EVERY = 120  # seconds
MIN_CONF_MULTI = 0.60

HELP = (
    "🤖 *PocketOption Manual Signal Bot*\n"
    "• Nothing runs until you type a command.\n\n"
    "Account/Session:\n"
    "/pologin SSID  – Save/verify your session (admin)\n"
    "/postatus      – Show SSID status\n\n"
    "Signals & Scans (manual only; no auto-trading):\n"
    "/mode strict|active|both|ultra\n"
    "/check SYMBOL [tf=1min]\n"
    "/autosignal SYMBOL AMOUNT DURATION [every=120] [tf=1min]\n"
    "/stopsignal\n"
    "/multisignal AMOUNT DURATION [tf=1min]\n"
    "/autosignalmulti PAIRS AMOUNT DURATION [every=120] [tf=1min] [minconf=60]\n"
    "/stopmultisignal\n\n"
    "Watchlist:\n"
    "/watch add|remove|list|clear [SYMBOL]\n\n"
    "Results & Stats:\n"
    "/result win|loss (auto-stop after 3 losses)\n"
    "/stats, /reset\n"
)

# ===== INDICATORS =====
def ema(values, period):
    if len(values) < period: return None
    k = 2/(period+1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v*k + e*(1-k)
    return e

def rsi(values, period=14):
    if len(values) <= period: return None
    gains, losses = [], []
    for i in range(1,len(values)):
        ch = values[i]-values[i-1]
        gains.append(max(ch,0)); losses.append(max(-ch,0))
    ag = sum(gains[:period])/period
    al = sum(losses[:period])/period
    for i in range(period,len(values)-1):
        ag = (ag*(period-1)+gains[i])/period
        al = (al*(period-1)+losses[i])/period
    if al == 0: return 100.0
    rs = ag/al
    return 100 - (100/(1+rs))

def atr_from_candles(candles, period=14):
    if len(candles) <= period: return None
    highs  = [float(c["high"]) for c in candles]
    lows   = [float(c["low"])  for c in candles]
    closes = [float(c["close"]) for c in candles]
    trs=[]
    for i in range(1,len(candles)):
        tr=max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
        trs.append(tr)
    k=2/(period+1); atr=sum(trs[:period])/period
    for v in trs[period:]:
        atr = v*k + atr*(1-k)
    return atr

def median_body(bodies):
    s=sorted(bodies); n=len(s)
    return (s[n//2] if n%2 else (s[n//2-1]+s[n//2])/2.0) if n else 0.0

# ===== SYMBOL HELPERS =====
def td_symbol(sym: str) -> str:
    raw = sym.upper().replace("_","")
    raw = raw[:-4] if raw.endswith("-OTC") else raw
    if "/" in raw: return raw
    return f"{raw[:3]}/{raw[3:6]}" if len(raw)>=6 else raw

def alpha_from_to(sym: str):
    raw = sym.upper().replace("/","")
    raw = raw[:-4] if raw.endswith("-OTC") else raw
    base = raw[:3]; quote = raw[3:6] if len(raw)>=6 else "USD"
    return base, quote

# ===== DATA FETCH =====
async def fetch_candles_twelve(symbol: str, interval="1min", limit=120):
    if not TWELVE_KEY: return [], "Missing TWELVE_KEY"
    url = f"https://api.twelvedata.com/time_series?symbol={td_symbol(symbol)}&interval={interval}&outputsize={limit}&apikey={TWELVE_KEY}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=20) as r:
                js = await r.json()
                if isinstance(js, dict) and js.get("status")=="error":
                    return [], f"Twelve error: {js.get('message')}"
                vals = js.get("values") or []
                # Convert to candle dicts (open/high/low/close)
                candles = []
                for v in reversed(vals):
                    candles.append({
                        "datetime": v.get("datetime"),
                        "open": float(v["open"]),
                        "high": float(v["high"]),
                        "low":  float(v["low"]),
                        "close":float(v["close"])
                    })
                return candles[-limit:], None
    except Exception as e:
        return [], f"Twelve fetch failed: {e}"

async def fetch_candles_alpha(symbol: str, interval="1min", limit=120):
    if not ALPHA_KEY: return [], "Missing ALPHA_KEY"
    base, quote = alpha_from_to(symbol)
    if interval not in {"1min","5min","15min","30min","60min"}: interval = "1min"
    url = ("https://www.alphavantage.co/query?"
           f"function=FX_INTRADAY&from_symbol={base}&to_symbol={quote}"
           f"&interval={interval}&apikey={ALPHA_KEY}&outputsize=compact")
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=20) as r:
                js = await r.json()
                ts = next((v for k,v in js.items() if "Time Series" in k), None)
                if not ts: return [], f"Alpha error: {js.get('Note') or js.get('Error Message') or 'no series'}"
                items = sorted(ts.items())
                candles=[]
                for t,v in items:
                    candles.append({
                        "datetime": t,
                        "open": float(v["1. open"]),
                        "high": float(v["2. high"]),
                        "low":  float(v["3. low"]),
                        "close":float(v["4. close"])
                    })
                return candles[-limit:], None
    except Exception as e:
        return [], f"Alpha fetch failed: {e}"

async def fetch_candles(symbol: str, interval="1min", limit=120):
    c, err = await fetch_candles_twelve(symbol, interval, limit)
    if c and not err: return c, None
    c2, err2 = await fetch_candles_alpha(symbol, interval, limit)
    if c2 and not err2: return c2, None
    return [], (err or err2 or "no data")

# ===== STRATEGY / SCORING =====
def decide_signal(closes, mode="both"):
    if len(closes) < 60: return None
    e50 = ema(closes,50); r = rsi(closes,14)
    if e50 is None or r is None: return None
    last = closes[-1]
    def strict():
        r_prev = rsi(closes[:-1],14)
        if r_prev is None: return None
        if last > e50 and r_prev < 30 <= r: return "call"
        if last < e50 and r_prev > 70 >= r: return "put"
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
        # momentum boost: last body >= median of last 10 bodies
        body = abs(closes[-1]-closes[-2])
        bodies = [abs(closes[i]-closes[i-1]) for i in range(-11,-1)]
        mom_th = median_body(bodies)
        return d if body >= mom_th else None
    return None

def score_probability(candles):
    if len(candles)<60: return (None,0.0,{})
    closes=[c["close"] for c in candles]
    opens =[c["open"]  for c in candles]
    e50=ema(closes,50); r=rsi(closes,14)
    if e50 is None or r is None: return (None,0.0,{})
    bodies=[abs(closes[i]-opens[i]) for i in range(-11,-1)]
    med = sorted(bodies)[5] if len(bodies)>=10 else 0.0
    mom = (abs(closes[-1]-opens[-1])/(med+1e-9)) if med else 0.0
    trend_call = 1.0 if closes[-1]>e50 else 0.0
    trend_put  = 1.0 - trend_call
    rsi_call = max(0.0,(30-r)/30); rsi_put = max(0.0,(r-70)/30)
    mom_boost=min(mom,2.0)/2.0
    call_score=0.45*rsi_call+0.40*trend_call+0.15*mom_boost
    put_score =0.45*rsi_put +0.40*trend_put +0.15*mom_boost
    if call_score<0.35 and put_score<0.35: return (None,0.0,{"weak":True})
    if call_score>=put_score:
        prob=max(0.5,min(0.85,0.5+(call_score-put_score)))
        return ("call",prob,{})
    else:
        prob=max(0.5,min(0.85,0.5+(put_score-call_score)))
        return ("put",prob,{})

def _dir_to_arrow(d): return "UP" if d=="call" else "DOWN"

async def htf_trend_ok(symbol: str, tf="15min", lookback=120):
    candles, err = await fetch_candles(symbol, tf, lookback)
    if err or len(candles)<55: return None
    closes=[c["close"] for c in candles]
    e50=ema(closes,50)
    if e50 is None: return None
    return "up" if closes[-1]>e50 else "down"

# ===== PO LOGIN / VERIFY =====
async def verify_ssid_http(ssid: str) -> bool:
    """
    Lightweight SSID check: hit a benign page sending the cookie.
    We include both Cookie: ssid=<val> and sessionToken=<val> for safety.
    """
    test_url = "https://pocketoption.com/en/cabinet/"
    try:
        async with aiohttp.ClientSession() as s:
            headers = {
                "Cookie": f"ssid={ssid}; sessionToken={ssid}",
                "User-Agent": "Mozilla/5.0"
            }
            async with s.get(test_url, timeout=12, headers=headers) as r:
                return 200 <= r.status < 400
    except Exception as e:
        logging.warning(f"SSID verify err: {e}")
        return False

# ===== COMMANDS =====
def is_admin(update: Update) -> bool:
    try:
        return int(update.effective_user.id) == int(ADMIN_ID)
    except Exception:
        return False

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Bot online. Nothing runs until you command it.\n" + HELP, parse_mode="Markdown")

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP, parse_mode="Markdown")

async def pologin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return await update.message.reply_text("❌ Not authorized.")
    if not ctx.args: return await update.message.reply_text("Usage: /pologin SSID")
    ssid = ctx.args[0].strip()
    ok = await verify_ssid_http(ssid)
    PO["ssid"] = ssid
    PO["verified"] = bool(ok)
    PO["saved_at"] = datetime.now(timezone.utc).isoformat()
    _save_state()
    if ok: await update.message.reply_text("✅ SSID saved & looks valid.")
    else:  await update.message.reply_text("⚠️ SSID saved, but validation failed (may be expired).")

async def postatus(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    mask = "(not set)"
    if PO.get("ssid"):
        v=PO["ssid"]
        mask = v[:4] + "…" + v[-4:] if len(v)>8 else v
    await update.message.reply_text(
        f"🔐 PO SSID: {mask}\nVerified: {PO.get('verified')}\nSaved: {PO.get('saved_at') or '-'}\nUID: {PO.get('uid') or '-'}"
    )

async def mode_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args or ctx.args[0].lower() not in ("strict","active","both","ultra"):
        return await update.message.reply_text("Usage: /mode strict|active|both|ultra")
    STATE["mode"]=ctx.args[0].lower()
    await update.message.reply_text(f"Mode set to {STATE['mode']}")

async def check_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) not in (1,2):
        return await update.message.reply_text("Usage: /check SYMBOL [tf=1min]")
    sym = ctx.args[0].upper()
    tf = ctx.args[1] if len(ctx.args)==2 else DEFAULT_TF
    candles, err = await fetch_candles(sym, tf, 120)
    if err or not candles: return await update.message.reply_text(f"❌ {err or 'no data'}")
    closes=[c["close"] for c in candles]
    decision = decide_signal(closes, STATE["mode"]) or "none"
    # Filters
    atr=atr_from_candles(candles,14)
    if decision and atr:
        last_body = abs(candles[-1]["close"]-candles[-1]["open"])
        if last_body < 0.6*atr: decision=None
    if decision:
        trend = await htf_trend_ok(sym,"15min",120)
        if trend is not None:
            want_up = (decision=="call")
            if (trend=="up" and not want_up) or (trend=="down" and want_up):
                decision=None
    # Confidence
    d, prob, _ = score_probability(candles)
    conf = int((prob or 0.0)*100)
    arrow = _dir_to_arrow(decision) if decision in ("call","put") else "-"
    await update.message.reply_text(
        f"📊 {sym} {tf}\nMode: {STATE['mode']}\nDecision: {decision.upper()}\nDirection: {arrow}\nConfidence: {conf}%\n\n➡️ Manual entry only"
    )

async def autosignal_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return await update.message.reply_text("❌ Not authorized.")
    if STATE["auto"]: return await update.message.reply_text("Already running. /stopsignal first.")
    if len(ctx.args) not in (3,4,5):
        return await update.message.reply_text(
            "Usage: /autosignal SYMBOL AMOUNT DURATION [every=120] [tf=1min]\n"
            "Example: /autosignal EURUSD-OTC 5 60 120 1min"
        )
    sym = ctx.args[0].upper()
    amount = float(ctx.args[1]); duration = int(ctx.args[2])
    every = int(ctx.args[3]) if len(ctx.args)>=4 else DEFAULT_EVERY
    tf = ctx.args[4] if len(ctx.args)==5 else DEFAULT_TF

    STATE["auto"]=True

    async def loop(chat_id):
        await ctx.bot.send_message(chat_id, f"▶️ Auto-signal {sym} | ${amount} | {duration}s | every {every}s | TF {tf} | mode {STATE['mode']}")
        while STATE["auto"] and STATE["daily_losses"]<LOSS_LIMIT:
            candles, err = await fetch_candles(sym, tf, 120)
            if not err and candles:
                closes=[c["close"] for c in candles]
                dec = decide_signal(closes, STATE["mode"])
                # ATR + HTF filters
                if dec:
                    atr=atr_from_candles(candles,14)
                    if not atr or abs(candles[-1]["close"]-candles[-1]["open"]) < 0.6*atr:
                        dec=None
                if dec:
                    trend = await htf_trend_ok(sym,"15min",120)
                    if trend is not None:
                        want_up=(dec=="call")
                        if (trend=="up" and not want_up) or (trend=="down" and want_up):
                            dec=None
                if dec:
                    _, prob, _ = score_probability(candles)
                    conf = int((prob or 0.0)*100)
                    arrow=_dir_to_arrow(dec)
                    await ctx.bot.send_message(chat_id,
                        f"📣 SIGNAL\nPair: {sym}\nDirection: {arrow}\nAmount: ${amount}\nDuration: {duration}s\nConfidence: {conf}%\n\n➡️ Place manually in Pocket Option.")
            await asyncio.sleep(every)
        STATE["auto"]=False
        await ctx.bot.send_message(chat_id,"⏹ Auto-signal stopped.")
    STATE["task"]=asyncio.create_task(loop(update.effective_chat.id))
    await update.message.reply_text("Auto started.")

async def stopsignal_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["auto"]=False
    await update.message.reply_text("Stopping auto-signal...")

async def multisignal_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) not in (2,3):
        return await update.message.reply_text("Usage: /multisignal AMOUNT DURATION [tf=1min]")
    amount=float(ctx.args[0]); duration=int(ctx.args[1])
    tf = ctx.args[2] if len(ctx.args)==3 else DEFAULT_TF
    hits=[]
    for pair in WATCHLIST:
        candles, err = await fetch_candles(pair, tf, 120)
        if err or not candles: continue
        closes=[c["close"] for c in candles]
        dec = decide_signal(closes, STATE["mode"])
        if dec:
            atr=atr_from_candles(candles,14)
            if (not atr) or abs(candles[-1]["close"]-candles[-1]["open"]) < 0.6*atr:
                dec=None
        if dec:
            trend = await htf_trend_ok(pair,"15min",120)
            if trend is not None:
                want_up=(dec=="call")
                if (trend=="up" and not want_up) or (trend=="down" and want_up):
                    dec=None
        if dec:
            _, prob, _ = score_probability(candles)
            conf=int((prob or 0.0)*100)
            hits.append(f"{pair}: {_dir_to_arrow(dec)} ({conf}% conf.)")
        await asyncio.sleep(0.2)
    if hits:
        await update.message.reply_text("📊 Multi-signal one-shot:\n" + "\n".join(hits) + f"\n\nSuggested: ${amount} / {duration}s")
    else:
        await update.message.reply_text("No clear setups across watchlist right now.")

async def autosignalmulti_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): return await update.message.reply_text("❌ Not authorized.")
    if STATE["multirun"]: return await update.message.reply_text("Already running. /stopmultisignal first.")
    if len(ctx.args) < 3:
        return await update.message.reply_text(
            "Usage: /autosignalmulti PAIRS AMOUNT DURATION [every=120] [tf=1min] [minconf=60]\n"
            "Example: /autosignalmulti EURUSD-OTC,GBPUSD-OTC 5 60 120 1min 65"
        )
    pairs_csv = ctx.args[0]; amount=float(ctx.args[1]); duration=int(ctx.args[2])
    every = int(ctx.args[3]) if len(ctx.args)>=4 else DEFAULT_EVERY
    tf = ctx.args[4] if len(ctx.args)>=5 else DEFAULT_TF
    minconf = int(ctx.args[5]) if len(ctx.args)>=6 else int(MIN_CONF_MULTI*100)
    min_conf_float = max(0.0, min(1.0, minconf/100.0))
    PAIRS=[p.strip().upper() for p in pairs_csv.split(",") if p.strip()]
    if not PAIRS: return await update.message.reply_text("Give me at least one pair.")

    STATE["multirun"]=True

    async def loop(chat_id):
        await ctx.bot.send_message(chat_id,
            f"▶️ Multi Auto-signal\nPairs: {', '.join(PAIRS)}\nStake: ${amount} | Expiry: {duration}s\nEvery: {every}s | TF: {tf}\nMin confidence: {int(min_conf_float*100)}%\nMode: {STATE['mode']}"
        )
        while STATE["multirun"] and STATE["daily_losses"]<LOSS_LIMIT:
            any_sent=False
            for sym in PAIRS:
                candles, err = await fetch_candles(sym, tf, 120)
                if err or not candles: 
                    await asyncio.sleep(0.05); continue
                closes=[c["close"] for c in candles]
                dec = decide_signal(closes, STATE["mode"])
                if dec:
                    atr=atr_from_candles(candles,14)
                    if not atr or abs(candles[-1]["close"]-candles[-1]["open"]) < 0.6*atr:
                        dec=None
                if dec:
                    trend = await htf_trend_ok(sym,"15min",120)
                    if trend is not None:
                        want_up=(dec=="call")
                        if (trend=="up" and not want_up) or (trend=="down" and want_up):
                            dec=None
                if dec:
                    _, prob, _ = score_probability(candles)
                    if (prob or 0.0) < min_conf_float:
                        dec=None
                if dec:
                    any_sent=True
                    conf=int((prob or 0.0)*100)
                    await ctx.bot.send_message(chat_id,
                        f"📣 SIGNAL\nPair: {sym}\nDirection: {_dir_to_arrow(dec)}\nAmount: ${amount}\nDuration: {duration}s\nConfidence: {conf}%\n\n➡️ Place manually.")
                await asyncio.sleep(0.15)
            await asyncio.sleep(every)
        STATE["multirun"]=False
        await ctx.bot.send_message(chat_id,"⏹ Multi auto-signal stopped.")
    STATE["multitask"]=asyncio.create_task(loop(update.effective_chat.id))
    await update.message.reply_text("Multi auto started.")

async def stopmultisignal_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE["multirun"]=False
    await update.message.reply_text("Stopping multi auto-signal...")

# Watchlist mgmt
async def watch_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sub=(ctx.args[0].lower() if ctx.args else "list")
    if sub=="add" and len(ctx.args)>=2:
        sym=ctx.args[1].upper()
        if sym not in WATCHLIST: WATCHLIST.append(sym)
        return await update.message.reply_text("Added. " + ", ".join(WATCHLIST))
    if sub=="remove" and len(ctx.args)>=2:
        sym=ctx.args[1].upper()
        if sym in WATCHLIST: WATCHLIST.remove(sym)
        return await update.message.reply_text("Removed. " + (", ".join(WATCHLIST) if WATCHLIST else "(empty)"))
    if sub=="clear":
        WATCHLIST.clear(); return await update.message.reply_text("Watchlist cleared.")
    return await update.message.reply_text(", ".join(WATCHLIST) if WATCHLIST else "(empty)")

# Results & Stats
async def result_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args or ctx.args[0].lower() not in ("win","loss"):
        return await update.message.reply_text("Usage: /result win|loss")
    res=ctx.args[0].lower()
    if res=="win":
        STATE["wins"]+=1
    else:
        STATE["losses"]+=1
        STATE["daily_losses"]+=1
    STATE["total"]+=1
    if STATE["daily_losses"]>=LOSS_LIMIT:
        STATE["auto"]=False; STATE["multirun"]=False
        await update.message.reply_text("🛑 Loss limit reached (3). All autos stopped.")
    await stats_cmd(update, ctx)

async def stats_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tot=STATE["wins"]+STATE["losses"]
    wr=(STATE["wins"]/tot*100) if tot else 0.0
    await update.message.reply_text(
        f"📈 Stats\nWins: {STATE['wins']}  Losses: {STATE['losses']}  WR: {wr:.1f}%\nDaily losses: {STATE['daily_losses']}/{LOSS_LIMIT}"
    )

async def reset_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATE.update({"wins":0,"losses":0,"total":0,"daily_losses":0})
    await update.message.reply_text("🔄 Stats reset.")

def main():
    import telegram
    logging.info(f"python-telegram-bot version: {telegram.__version__}")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("pologin", pologin))
    app.add_handler(CommandHandler("postatus", postatus))
    app.add_handler(CommandHandler("mode", mode_cmd))
    app.add_handler(CommandHandler("check", check_cmd))
    app.add_handler(CommandHandler("autosignal", autosignal_cmd))
    app.add_handler(CommandHandler("stopsignal", stopsignal_cmd))
    app.add_handler(CommandHandler("multisignal", multisignal_cmd))
    app.add_handler(CommandHandler("autosignalmulti", autosignalmulti_cmd))
    app.add_handler(CommandHandler("stopmultisignal", stopmultisignal_cmd))
    app.add_handler(CommandHandler("watch", watch_cmd))
    app.add_handler(CommandHandler("result", result_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))

    # Nothing auto-runs: polling only starts the bot. Scans begin only after commands.
    app.run_polling()

if __name__ == "__main__":
    main()