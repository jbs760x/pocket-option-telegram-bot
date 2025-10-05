# pocket-option-telegram-bot — Render-ready (PTB v20), OTC + confidence + PO tracking
# Stops ONLY after 3 CONSECUTIVE losses (no win limit)

import os, re, json, logging, asyncio, aiohttp, time, socketio
from collections import deque
from datetime import datetime, timezone, timedelta
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# === YOUR CREDENTIALS (from your screenshots) ===
BOT_TOKEN  = "8471181182:AAFEhPc59AvzNsnuPbj-N2PatGbvgZnnd_0"
ADMIN_ID   = 7814662315
TWELVE_KEY = "9aa4ea677d00474aa0c3223d0c812425"
ALPHA_KEY  = "BM22MZEIOLL68RI6"

# Pocket Option socket (sessionToken/SSID + region from your console)
PO_SSID   = "d7a8a43d4618a7227c6ed769f8fd9975"
PO_REGION = "us-south"   # us-south | us-north | msk

# === Strategy / limits ===
STRATEGY_MODE = "both"     # strict|active|mean|both
LEAD_SEC      = 60
COOLDOWN_SEC  = 240
MAX_PER_HOUR  = 6
PAYOUT_MIN    = 0.75
WATCHLIST     = ["EURUSD-OTC","GBPUSD-OTC","USDJPY-OTC","AUDCAD-OTC"]

# === State ===
STATS = {"wins":0,"losses":0,"entries_this_series":0,"total_signals":0}
LOSS_STREAK = 0      # ← we stop at 3 consecutive losses
LAST_FIRES = {}      # cooldown buckets
AUTO = {"running": False, "task": None}

PO_CONNECTED = False
PO_EVENTS_LAST = deque(maxlen=50)

_last_signal_bar_index = None

HELP_TEXT = (
    "🤖 Signals\n"
    "/start, /help\n"
    "/mode strict|active|mean|both\n"
    "/check SYMBOL [tf=1min]\n"
    "/signal SYMBOL call|put AMOUNT DURATION\n"
    "/autosignal SYMBOL AMOUNT DURATION [every=120] [tf=1min]\n"
    "/stopsignal\n\n"
    "🛰 Pocket Option\n"
    "/pologin – connect WS with your SSID\n"
    "/poinfo – socket status\n"
    "/poevents – last socket events\n\n"
    "📊 Stats\n"
    "/stats, /resetstats\n\n"
    "OTC example: EURUSD-OTC"
)

# -------- utils & indicators --------
def _dir_to_arrow(direction: str) -> str: return "UP" if direction.lower()=="call" else "DOWN"

def parse_line(txt: str):
    m=re.match(r"^\s*([A-Za-z0-9/_\-.]+)\s+(call|put)\s+(\d+(?:\.\d+)?)\s+(\d+)\s*$", txt, re.I)
    if not m: return None
    s,d,a,dur=m.groups(); return s.upper(), d.lower(), float(a), int(dur)

def ema(values, period):
    if len(values) < period: return None
    k = 2/(period+1); ev = sum(values[:period]) / period
    for v in values[period:]: ev = v*k + ev*(1-k)
    return ev

def rsi(values, period=14):
    if len(values) <= period: return None
    gains, losses = [], []
    for i in range(1,len(values)):
        ch = values[i]-values[i-1]
        gains.append(max(ch,0)); losses.append(max(-ch,0))
    avg_g = sum(gains[:period])/period; avg_l = sum(losses[:period])/period
    for i in range(period, len(values)-1):
        avg_g = (avg_g*(period-1)+gains[i])/period
        avg_l = (avg_l*(period-1)+losses[i])/period
    if avg_l == 0: return 100.0
    rs = avg_g/avg_l
    return 100 - (100/(1+rs))

def atr_from_candles(candles, period=14):
    if len(candles) <= period: return None
    highs=[float(c["high"]) for c in candles]
    lows=[float(c["low"]) for c in candles]
    closes=[float(c["close"]) for c in candles]
    trs=[]
    for i in range(1,len(candles)):
        trs.append(max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])))
    k=2/(period+1); atr=sum(trs[:period])/period
    for v in trs[period:]: atr=v*k+atr*(1-k)
    return atr

def norm_symbol_to_twelve(sym: str) -> str:
    raw = sym.upper()
    pure = raw[:-4] if raw.endswith("-OTC") else raw
    pure = pure.replace("_","")
    if "/" in pure: return pure
    return f"{pure[:3]}/{pure[3:]}" if len(pure)==6 else pure

def display_and_fetch_symbol(sym: str): return sym.upper(), norm_symbol_to_twelve(sym)

def alpha_from_to(sym: str):
    raw=sym.upper(); raw = raw[:-4] if raw.endswith("-OTC") else raw
    raw=raw.replace("/","")
    base=raw[:3]; quote=raw[3:6] if len(raw)>=6 else (raw[3:] or "USD")
    return base, quote

def next_bar_seconds(interval: str) -> int:
    mins = int(interval.replace("min",""))
    now = datetime.now(timezone.utc)
    bucket = (now.minute // mins + 1) * mins
    nxt = now.replace(second=0, microsecond=0)
    nxt = (nxt + timedelta(hours=1)).replace(minute=0) if bucket>=60 else nxt.replace(minute=bucket)
    return max(0, int((nxt-now).total_seconds()))

# -------- data providers --------
async def _fetch_candles_twelve(symbol: str, interval="1min", limit=120):
    if not TWELVE_KEY: return [], "Missing TWELVE_KEY"
    _, td_symbol = display_and_fetch_symbol(symbol)
    url=(f"https://api.twelvedata.com/time_series?symbol={td_symbol}&interval={interval}"
         f"&outputsize={limit}&apikey={TWELVE_KEY}")
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=20) as r:
                if r.status!=200: return [], f"Twelve HTTP {r.status}"
                js=await r.json()
                if isinstance(js,dict) and js.get("status")=="error": return [], f"Twelve error: {js.get('message','unknown')}"
                vals=js.get("values"); 
                if not vals: return [], "Twelve no candles"
                return list(reversed(vals))[-limit:], None
    except Exception as e:
        return [], f"Twelve fetch failed: {e}"

async def fetch_candles_alpha(symbol: str, interval="1min", limit=120):
    if not ALPHA_KEY: return [], "Missing ALPHAVANTAGE_KEY"
    base, quote = alpha_from_to(symbol)
    if interval not in {"1min","5min","15min","30min","60min"}: interval="1min"
    url=("https://www.alphavantage.co/query?"
         f"function=FX_INTRADAY&from_symbol={base}&to_symbol={quote}&interval={interval}"
         f"&apikey={ALPHA_KEY}&outputsize=compact")
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=20) as r:
                js=await r.json()
                ts_key=next((k for k in js.keys() if "Time Series" in k), None)
                if not ts_key: return [], f"Alpha error: {js.get('Note') or js.get('Error Message') or 'unknown'}"
                series=js[ts_key]; candles=[]
                for t,v in sorted(series.items()):
                    candles.append({"datetime":t,"open":float(v["1. open"]),
                                    "high":float(v["2. high"]),"low":float(v["3. low"]),
                                    "close":float(v["4. close"])})
                return candles[-limit:], None
    except Exception as e:
        return [], f"Alpha fetch failed: {e}"

PROVIDERS = {"twelve": _fetch_candles_twelve, "alpha": fetch_candles_alpha}
DATA_ORDER = ["twelve","alpha"]

async def fetch_candles(symbol, interval="1min", limit=120):
    errs=[]
    for name in DATA_ORDER:
        candles, err = await PROVIDERS[name](symbol, interval, limit)
        if candles and not err:
            logging.info("[DATA] %s used for %s %s", name, symbol, interval)
            return candles, None
        errs.append(f"{name}: {err or 'no data'}")
    return [], " | ".join(errs)

# -------- strategy & confidence --------
def decide_signal_standard(closes):
    if len(closes) < 60: return None
    last=closes[-1]; e50=ema(closes,50); r=rsi(closes,14)
    if e50 is None or r is None: return None
    r_prev=rsi(closes[:-1],14)
    def strict():
        if r_prev is None: return None
        if last>e50 and r_prev<30<=r: return "call"
        if last<e50 and r_prev>70>=r: return "put"
        return None
    def active():
        if last>e50 and r>55: return "call"
        if last<e50 and r<45: return "put"
        return None
    def mean():
        if r>=70: return "put"
        if r<=30: return "call"
        return None
    if STRATEGY_MODE=="strict": return strict()
    if STRATEGY_MODE=="active": return active()
    if STRATEGY_MODE=="mean":   return mean()
    if STRATEGY_MODE=="both":   return strict() or active()
    return None

def median_body(bodies): s=sorted(bodies); n=len(s); return (s[n//2] if n%2 else (s[n//2-1]+s[n//2])/2.0) if n else 0.0

def score_probability(candles):
    if len(candles)<60: return (None,0.0,{})
    closes=[float(c["close"]) for c in candles]
    opens =[float(c["open"]) for c in candles]
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
        prob=max(0.5,min(0.8,0.5+(call_score-put_score))); return ("call",prob,{})
    prob=max(0.5,min(0.8,0.5+(put_score-call_score))); return ("put",prob,{})

def _confidence_for_direction(decision, candles):
    try:
        d, prob, _ = score_probability(candles)
        if decision and d and decision==d: return float(prob)
        return float(prob) if prob>0 else None
    except: return None

# -------- guards --------
def _cooldown_ok(pair: str)->bool:
    now=time.time(); q=LAST_FIRES.setdefault(pair, deque())
    while q and now-q[0]>3600: q.popleft()
    if q and (now-q[-1] < COOLDOWN_SEC): return False
    if len(q) >= MAX_PER_HOUR: return False
    q.append(now); return True

async def htf_trend_ok(symbol: str, tf="15min", lookback=120):
    candles, err = await fetch_candles(symbol, tf, lookback)
    if err or len(candles)<55: return None
    closes=[float(c["close"]) for c in candles]; e50=ema(closes,50)
    if e50 is None: return None
    return "up" if closes[-1]>e50 else "down"

# -------- Pocket Option socket.io tracking --------
_sio = socketio.AsyncClient(reconnection=True, logger=False, engineio_logger=False)

async def _po_connect_forever():
    base=f"https://api-{PO_REGION}.po.market"

    @_sio.event
    async def connect():
        global PO_CONNECTED
        PO_CONNECTED=True
        logging.info("PO WS connected, sending auth")
        await _sio.emit("auth", {"sessionToken": PO_SSID})

    @_sio.event
    async def disconnect():
        global PO_CONNECTED
        PO_CONNECTED=False
        logging.info("PO WS disconnected")

    @_sio.on("*")
    async def any_event(event, data):
        # store last 50 events for /poevents
        PO_EVENTS_LAST.append({"event":event, "data":data})
        # try to infer outcomes
        try:
            payload = data if isinstance(data, dict) else (data[0] if isinstance(data, list) else {})
            res = str(payload.get("result") or payload.get("status") or "").lower()
            if res in ("win","loss"):
                await _record_result(res)
        except Exception as e:
            logging.warning("PO parse err: %s", e)

    while True:
        try:
            if not _sio.connected:
                await _sio.connect(base, transports=["websocket"], wait_timeout=15)
            await _sio.wait()
        except Exception as e:
            logging.warning("PO connect error: %s", e)
            await asyncio.sleep(3)

# -------- results & stop-after-3-losses --------
async def _record_result(result: str):
    global LOSS_STREAK
    if result == "win":
        STATS["wins"] += 1
        LOSS_STREAK = 0          # reset on win
    elif result == "loss":
        STATS["losses"] += 1
        LOSS_STREAK += 1         # count consecutive losses

# -------- commands --------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot online. Type /help")

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)

async def mode_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global STRATEGY_MODE
    if not ctx.args or ctx.args[0].lower() not in ("strict","active","mean","both"):
        return await update.message.reply_text("Usage: /mode strict|active|mean|both")
    STRATEGY_MODE = ctx.args[0].lower()
    await update.message.reply_text(f"Mode: {STRATEGY_MODE}")

async def check_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) not in (1,2): return await update.message.reply_text("Usage: /check SYMBOL [tf=1min]")
    symbol=ctx.args[0].upper(); tf=ctx.args[1] if len(ctx.args)==2 else "1min"
    disp,_=display_and_fetch_symbol(symbol)
    candles, err = await fetch_candles(symbol, tf, 120)
    if err: return await update.message.reply_text(f"❌ {err}")
    closes=[float(c["close"]) for c in candles]
    e=ema(closes,50); r=rsi(closes,14); dec=decide_signal_standard(closes)
    await update.message.reply_text(f"📊 {disp} {tf}\nEMA50: {round(e,5) if e else 'n/a'}\nRSI14: {round(r,2) if r else 'n/a'}\nDecision: {dec or 'none'}")

async def signal_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if int(update.effective_user.id)!=int(ADMIN_ID): return await update.message.reply_text("Not authorized.")
    if len(ctx.args)!=4: return await update.message.reply_text("Usage: /signal SYMBOL call|put AMOUNT DURATION")
    symbol, direction, amount, duration = ctx.args[0].upper(), ctx.args[1].lower(), float(ctx.args[2]), int(ctx.args[3])
    candles, err = await fetch_candles(symbol, "1min", 120)
    if err: return await update.message.reply_text(f"❌ {err}")
    conf=_confidence_for_direction(direction, candles)
    arrow=_dir_to_arrow(direction); disp,_=display_and_fetch_symbol(symbol)
    await update.message.reply_text(f"✅ Signal\nPair: {disp}\nDirection: {arrow}\nAmount: ${amount}\nDuration: {duration}s" + (f"\nConfidence: {int(conf*100)}%" if conf is not None else ""))

async def autosignal_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if int(update.effective_user.id)!=int(ADMIN_ID): return await update.message.reply_text("Not authorized.")
    a=ctx.args
    if len(a) not in (3,4,5):
        return await update.message.reply_text("Usage: /autosignal SYMBOL AMOUNT DURATION [every=120] [tf=1min]")
    symbol=a[0].upper(); amount=float(a[1]); duration=int(a[2])
    every=int(a[3]) if len(a)>=4 else 120
    tf=a[4] if len(a)==5 else "1min"
    if AUTO["running"]: return await update.message.reply_text("Already running. /stopsignal first.")
    AUTO["running"]=True
    AUTO["task"]=asyncio.create_task(autosignal_loop(ctx, update.effective_chat.id, symbol, amount, duration, every, tf))
    await update.message.replyText if False else None  # keep linter calm

async def autosignal_loop(ctx, chat_id, symbol, amount, duration, every, tf):
    global LOSS_STREAK
    disp,_=display_and_fetch_symbol(symbol)
    await ctx.bot.send_message(chat_id, f"▶️ Auto {disp} | ${amount} | {duration}s | TF {tf} | every {every}s | mode {STRATEGY_MODE}")
    while AUTO["running"]:
        # stop after 3 consecutive losses
        if LOSS_STREAK >= 3:
            AUTO["running"]=False
            await ctx.bot.send_message(chat_id, "🛑 Stopping: 3 consecutive losses reached.")
            LOSS_STREAK = 0
            break

        candles, err = await fetch_candles(symbol, tf, 120)
        if err: await asyncio.sleep(every); continue

        closes=[float(c["close"]) for c in candles]
        decision=decide_signal_standard(closes)

        # ATR & 15m trend confirmation
        if decision:
            atr=atr_from_candles(candles,14)
            if not atr or abs(float(candles[-1]["close"])-float(candles[-1]["open"])) < 0.6*atr:
                decision=None
            if decision:
                trend = await htf_trend_ok(symbol, "15min", 120)
                want_up = (decision=="call")
                if trend is not None and ((trend=="up" and not want_up) or (trend=="down" and want_up)):
                    decision=None

        if decision:
            disp,_=display_and_fetch_symbol(symbol)
            if not _cooldown_ok(disp): decision=None

        if decision:
            conf=_confidence_for_direction(decision, candles)
            arrow=_dir_to_arrow(decision)
            wait=next_bar_seconds(tf); lead=min(LEAD_SEC, wait); eta=max(0, wait-lead)
            conf_txt = f"\nConfidence: {int(conf*100)}%" if conf is not None else ""
            await ctx.bot.send_message(chat_id, f"📣 Upcoming ({tf})\nPair: {disp}\nDirection: {arrow}{conf_txt}\nPlace at: next open (~{wait}s)\nExpiry: {duration}s")
            if eta>0: await asyncio.sleep(eta)
            if lead>0:
                await ctx.bot.send_message(chat_id, f"⏱ Get ready: **{arrow}** on {disp} in ~{lead}s")
                await asyncio.sleep(lead)
            STATS["entries_this_series"]+=1; STATS["total_signals"]+=1
            await ctx.bot.send_message(chat_id, f"✅ PLACE NOW\nPair: {disp}\nDirection: {arrow}\nAmount: ${amount}\nDuration: {duration}s{conf_txt}")

        await asyncio.sleep(every)
    await ctx.bot.send_message(chat_id, "⏹ Auto-signal stopped.")

async def stopsignal_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    AUTO["running"]=False
    await update.message.reply_text("Stopping auto-signal...")

async def stats_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tot=STATS["wins"]+STATS["losses"]; wr=(STATS["wins"]/tot*100) if tot else 0.0
    await update.message.reply_text(
        f"📊 Stats\nSignals: {STATS['total_signals']}\nWins: {STATS['wins']}  Losses: {STATS['losses']}  WR: {wr:.1f}%\nLoss streak: {LOSS_STREAK}"
    )

async def resetstats_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global LOSS_STREAK
    STATS.update({"wins":0,"losses":0,"entries_this_series":0,"total_signals":0})
    LOSS_STREAK=0
    await update.message.reply_text("✅ Stats reset.")

# ----- Pocket Option control commands -----
async def pologin_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔌 Connecting to Pocket Option WS…")
    asyncio.create_task(_po_connect_forever())

async def poinfo_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"PO socket: {'✅ connected' if _sio.connected else '❌ disconnected'} | region: {PO_REGION}")

async def poevents_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not PO_EVENTS_LAST:
        return await update.message.reply_text("(no events yet)")
    lines=[]
    for e in list(PO_EVENTS_LAST)[-10:]:
        ev=str(e.get('event'))
        lines.append(f"• {ev}")
    await update.message.reply_text("Last events:\n" + "\n".join(lines))

# ----- text parser (manual signals like: 'EURUSD-OTC call 5 60') -----
async def echo_parse(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    parsed = parse_line(update.message.text)
    if not parsed: return
    if int(update.effective_user.id)!=int(ADMIN_ID):
        return await update.message.reply_text("Not authorized.")
    symbol, direction, amount, duration = parsed
    candles, err = await fetch_candles(symbol, "1min", 120)
    conf = None if err else _confidence_for_direction(direction, candles)
    arrow=_dir_to_arrow(direction)
    disp,_=display_and_fetch_symbol(symbol)
    await update.message.reply_text(
        f"✅ Manual signal\nPair: {disp}\nDirection: {arrow}\nAmount: ${amount}\nDuration: {duration}s" + (f"\nConfidence: {int(conf*100)}%" if conf is not None else "")
    )

# ----- main -----
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("mode", mode_cmd))
    app.add_handler(CommandHandler("check", check_cmd))
    app.add_handler(CommandHandler("signal", signal_cmd))
    app.add_handler(CommandHandler("autosignal", autosignal_cmd))
    app.add_handler(CommandHandler("stopsignal", stopsignal_cmd))
    app.add_handler(CommandHandler("pologin", pologin_cmd))
    app.add_handler(CommandHandler("poinfo", poinfo_cmd))
    app.add_handler(CommandHandler("poevents", poevents_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("resetstats", resetstats_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo_parse))
    app.run_polling()

if __name__=="__main__":
    main()