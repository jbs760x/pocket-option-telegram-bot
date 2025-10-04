# telegram_bot.py — Pocket Option integrated (OTC + real trades)
# Notes from your screenshots:
# - Uses the WebSocket auth/sessionToken you found via Network→WS "42['auth', {sessionToken:...}]"
# - Works on pocketoption (po.market) sockets like the ones you saw (api-us-*, api-msk.*)
# - No external worker needed; trades go directly via PocketOption API client

import os, re, json, logging, asyncio, aiohttp, time
from collections import deque
from datetime import datetime, timezone, timedelta
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ===== YOUR SETTINGS (kept from your file; edit token if needed) =====
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or "8471181182:AAFEhPc59AvzNsnuPbj-N2PatGbvgZnnd_0"
ADMIN_ID  = int(os.getenv("ADMIN_ID", "7814662315"))

# --- Your SSID from the photos (WebSocket auth message) ---
PO_SSID   = os.getenv("PO_SSID") or "d7a8a43d4618a7227c6ed769f8fd9975"  # << your token

# Optional REST worker (not required when trading via PO client)
WORKER_URL  = os.getenv("WORKER_URL", "")

# Market data fallbacks (kept from your bot)
TWELVE_KEY  = os.getenv("TWELVE_KEY", "9aa4ea677d00474aa0c3223d0c812425")
ALPHA_KEY   = os.getenv("ALPHAVANTAGE_KEY", "BM22MZEIOLL68RI6")
DATA_SOURCES_ENV = os.getenv("DATA_SOURCES", "pocket,twelve,alpha")  # pocket first

# ===== Pocket Option client (Chipa-style community lib) =====
# Make sure requirements.txt contains:
# git+https://github.com/lu-yi-hsun/pocketoptionapi.git
PO_CLIENT = None
try:
    from pocketoptionapi.stable_api import PocketOption
    _PO_LIB_OK = True
except Exception as e:
    logging.warning("PocketOptionAPI not installed yet: %s", e)
    _PO_LIB_OK = False

async def po_connect():
    global PO_CLIENT
    if not _PO_LIB_OK:
        raise RuntimeError("Install pocketoptionapi (see requirements.txt)")
    if not PO_SSID:
        raise RuntimeError("PO_SSID missing")
    def _connect():
        po = PocketOption(PO_SSID)
        ok, msg = po.connect()
        return po if ok else None, msg
    po, msg = await asyncio.to_thread(_connect)
    if not po: raise RuntimeError(f"PO connect failed: {msg}")
    PO_CLIENT = po
    logging.info("Pocket Option connected.")

def _tf_map(tf: str) -> str:
    return {"1min":"M1","5min":"M5","15min":"M15","30min":"M30","60min":"H1"}.get(tf.lower(), "M1")

async def po_get_candles(symbol: str, tf="1min", limit=120):
    if PO_CLIENT is None: raise RuntimeError("PO not connected. Run /pologin")
    def _pull():
        return PO_CLIENT.get_candles(symbol.upper(), _tf_map(tf), limit)
    data = await asyncio.to_thread(_pull)
    out=[]
    for c in data[-limit:]:
        o=float(c.get("open", c.get("o", 0))); h=float(c.get("high", c.get("h", o)))
        l=float(c.get("low", c.get("l", o)));  cl=float(c.get("close", c.get("c", o)))
        out.append({"open":o,"high":h,"low":l,"close":cl})
    return out

async def po_trade(symbol: str, direction: str, amount: float, duration_sec: int):
    if PO_CLIENT is None: raise RuntimeError("PO not connected. Run /pologin")
    side = "call" if direction.lower() in ("call","buy","up") else "put"
    def _buy():
        return PO_CLIENT.buy(symbol.upper(), float(amount), side, int(duration_sec))
    res = await asyncio.to_thread(_buy)
    return {"ok": bool(res), "raw": res}

# ===== Indicators / strategy (your original logic, kept compact) =====
def ema(vals,p): 
    if len(vals)<p: return None
    k=2/(p+1); e=sum(vals[:p])/p
    for v in vals[p:]: e=v*k+e*(1-k)
    return e

def rsi(values, period=14):
    if len(values)<=period: return None
    gains=[]; losses=[]
    for i in range(1,len(values)):
        ch=values[i]-values[i-1]
        gains.append(max(ch,0)); losses.append(max(-ch,0))
    ag=sum(gains[:period])/period; al=sum(losses[:period])/period
    for i in range(period,len(values)-1):
        ag=(ag*(period-1)+gains[i])/period
        al=(al*(period-1)+losses[i])/period
    if al==0: return 100.0
    rs=ag/al; return 100-(100/(1+rs))

def atr_from_candles(c, period=14):
    if len(c)<=period: return None
    highs=[float(x["high"]) for x in c]; lows=[float(x["low"]) for x in c]; closes=[float(x["close"]) for x in c]
    trs=[]
    for i in range(1,len(c)):
        trs.append(max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])))
    k=2/(period+1); a=sum(trs[:period])/period
    for v in trs[period:]: a=v*k+a*(1-k)
    return a

def decide_signal(closes, mode="both"):
    if len(closes)<60: return None
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
    if mode=="strict": return strict()
    if mode=="active": return active()
    if mode=="mean":   return "put" if r>=70 else ("call" if r<=30 else None)
    if mode=="both":   return strict() or active()
    return None

def score_probability(candles):
    if len(candles)<60: return (None,0.0)
    closes=[float(c["close"]) for c in candles]; opens=[float(c["open"]) for c in candles]
    e50=ema(closes,50); r=rsi(closes,14)
    if e50 is None or r is None: return (None,0.0)
    bodies=[abs(closes[i]-opens[i]) for i in range(-11,-1)]
    med=sorted(bodies)[5] if len(bodies)>=10 else 0.0
    mom=(abs(closes[-1]-opens[-1])/(med+1e-9)) if med else 0.0
    trend_call=1.0 if closes[-1]>e50 else 0.0; trend_put=1.0-trend_call
    rsi_call=max(0.0,(30-r)/30); rsi_put=max(0.0,(r-70)/30)
    mom_boost=min(mom,2.0)/2.0
    call_score=0.45*rsi_call+0.40*trend_call+0.15*mom_boost
    put_score =0.45*rsi_put +0.40*trend_put +0.15*mom_boost
    if call_score<0.35 and put_score<0.35: return (None,0.0)
    if call_score>=put_score: return ("call", max(0.5,min(0.8,0.5+(call_score-put_score))))
    return ("put", max(0.5,min(0.8,0.5+(put_score-call_score))))

def next_bar_seconds(interval: str) -> int:
    mins=int(interval.replace("min",""))
    now=datetime.now(timezone.utc)
    bucket=(now.minute//mins+1)*mins
    nxt=now.replace(second=0,microsecond=0)
    nxt=(nxt+timedelta(hours=1)).replace(minute=0) if bucket>=60 else nxt.replace(minute=bucket)
    return max(0,int((nxt-now).total_seconds()))

def _dir_to_arrow(d): return "UP" if d=="call" else "DOWN"

# ===== Limits / state (kept) =====
LEAD_SEC=60; STRATEGY_MODE="both"
COOLDOWN_SEC=240; MAX_PER_HOUR=6; LAST_FIRES={}
DAILY_MAX_WINS=3; DAILY_MAX_LOSSES=2
DAILY_COUNTER={"date":None,"wins":0,"losses":0}
STATS={"wins":0,"losses":0,"entries_this_series":0,"total_signals":0}

def _cooldown_ok(pair):
    now=time.time(); q=LAST_FIRES.setdefault(pair, deque())
    while q and now-q[0]>3600: q.popleft()
    if q and (now-q[-1] < COOLDOWN_SEC): return False
    if len(q) >= MAX_PER_HOUR: return False
    q.append(now); return True

def _daily_ensure_today():
    today=datetime.utcnow().date().__str__()
    if DAILY_COUNTER["date"]!=today: DAILY_COUNTER.update({"date":today,"wins":0,"losses":0})

async def _daily_stop_check(ctx, chat_id):
    _daily_ensure_today()
    if DAILY_COUNTER["wins"]>=DAILY_MAX_WINS or DAILY_COUNTER["losses"]>=DAILY_MAX_LOSSES:
        await ctx.bot.send_message(chat_id,"🛑 Daily stop reached.")
        return True
    return False

# ===== Data providers (Pocket first; Twelve/Alpha fallback kept) =====
async def fetch_candles_provider(symbol: str, interval="1min", limit=120):
    order=[p.strip() for p in DATA_SOURCES_ENV.split(",") if p.strip()]
    errs=[]
    for prov in order:
        try:
            if prov=="pocket":
                candles = await po_get_candles(symbol, interval, limit)
                if candles: return candles, None
            elif prov=="twelve":
                _, td = symbol, symbol.replace("-OTC","").replace("_","/")
                url=f"https://api.twelvedata.com/time_series?symbol={td}&interval={interval}&outputsize={limit}&apikey={TWELVE_KEY}"
                async with aiohttp.ClientSession() as s:
                    async with s.get(url, timeout=20) as r:
                        js=await r.json()
                        vals=js.get("values") if isinstance(js,dict) else None
                        if vals: return list(reversed(vals))[-limit:], None
                        errs.append(f"twelve:{js.get('message')}")
            elif prov=="alpha":
                raw=symbol.replace("-OTC","").replace("/","")
                base,quote=(raw[:3], raw[3:6] or "USD")
                iv=interval if interval in {"1min","5min","15min","30min","60min"} else "1min"
                url=("https://www.alphavantage.co/query?"
                     f"function=FX_INTRADAY&from_symbol={base}&to_symbol={quote}&interval={iv}"
                     f"&apikey={ALPHA_KEY}&outputsize=compact")
                async with aiohttp.ClientSession() as s:
                    async with s.get(url, timeout=20) as r:
                        js=await r.json()
                        k=next((k for k in js if "Time Series" in k), None)
                        if k:
                            series=js[k]; out=[]
                            for t,v in sorted(series.items()):
                                out.append({"open":float(v["1. open"]),"high":float(v["2. high"]),
                                            "low":float(v["3. low"]),"close":float(v["4. close"])})
                            if out: return out[-limit:], None
                        errs.append("alpha:no series")
        except Exception as e:
            errs.append(f"{prov}:{e}")
    return [], " | ".join(errs) if errs else "no data"

# ===== Telegram commands =====
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("OTC bot ready.\n/pologin to connect, then /signal EURUSD-OTC 5 60")

async def pologin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        await po_connect()
        await update.message.reply_text("✅ Pocket Option connected.")
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")

async def check_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) not in (1,2): return await update.message.reply_text("Use: /check SYMBOL [tf=1min]")
    sym=ctx.args[0].upper(); tf=ctx.args[1] if len(ctx.args)==2 else "1min"
    candles, err = await fetch_candles_provider(sym, tf, 120)
    if err: return await update.message.reply_text(f"❌ {err}")
    closes=[float(c["close"]) for c in candles]
    e=ema(closes,50); r=rsi(closes,14); dec=decide_signal(closes, STRATEGY_MODE)
    await update.message.reply_text(f"📊 {sym} {tf}\nEMA50: {round(e,5) if e else 'n/a'}\nRSI14: {round(r,2) if r else 'n/a'}\nDecision: {dec or 'none'}")

async def signal_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if int(update.effective_user.id)!=int(ADMIN_ID): return await update.message.reply_text("Not authorized.")
    if len(ctx.args)!=4: return await update.message.reply_text("Use: /signal SYMBOL call|put AMOUNT DURATION")
    sym, direction, amount, duration = ctx.args[0].upper(), ctx.args[1].lower(), float(ctx.args[2]), int(ctx.args[3])
    candles, err = await fetch_candles_provider(sym, "1min", 120)
    if err: return await update.message.reply_text(f"❌ {err}")
    _, prob = score_probability(candles); conf=int(prob*100) if prob else 0
    res = await po_trade(sym, direction, amount, duration)
    arrow="UP" if direction=="call" else "DOWN"
    await update.message.reply_text(
        f"✅ Trade\nPair: {sym}\nDirection: {arrow}\nAmount: ${amount}\nDuration: {duration}s\nConfidence: {conf}%\nPlaced: {res.get('ok')}"
    )

AUTO={"running":False,"task":None}
async def autosignal_loop(ctx, chat_id, sym, amount, duration, every, tf):
    await ctx.bot.send_message(chat_id, f"▶️ Auto {sym} ${amount}/{duration}s every {every}s [{tf}]")
    while AUTO["running"]:
        if await _daily_stop_check(ctx, chat_id): break
        candles, err = await fetch_candles_provider(sym, tf, 120)
        if not err and candles:
            closes=[float(c["close"]) for c in candles]
            dec=decide_signal(closes, STRATEGY_MODE)
            atr=atr_from_candles(candles,14)
            if dec and atr and abs(float(candles[-1]['close'])-float(candles[-1]['open']))>=0.6*atr:
                if not _cooldown_ok(sym): pass
                else:
                    d=dec; _,prob=score_probability(candles); conf=int((prob or 0)*100)
                    wait=next_bar_seconds(tf); lead=min(60,wait); eta=max(0,wait-lead)
                    if eta: await asyncio.sleep(eta)
                    if lead: await ctx.bot.send_message(chat_id, f"⏱ {sym} {('BUY' if d=='call' else 'SELL')} in ~{lead}s ({conf}%)"); await asyncio.sleep(lead)
                    await po_trade(sym, d, amount, duration)
                    await ctx.bot.send_message(chat_id, f"✅ PLACE NOW {sym} {('BUY' if d=='call' else 'SELL')} ${amount}/{duration}s ({conf}%)")
        await asyncio.sleep(every)
    await ctx.bot.send_message(chat_id,"⏹ Auto stopped.")

async def autosignal_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if int(update.effective_user.id)!=int(ADMIN_ID): return await update.message.reply_text("Not authorized.")
    if len(ctx.args) not in (3,4,5): return await update.message.reply_text("Use: /autosignal SYMBOL AMOUNT DURATION [every=120] [tf=1min]")
    sym=ctx.args[0].upper(); amount=float(ctx.args[1]); duration=int(ctx.args[2])
    every=int(ctx.args[3]) if len(ctx.args)>=4 else 120; tf=ctx.args[4] if len(ctx.args)>=5 else "1min"
    if AUTO["running"]: return await update.message.reply_text("Already running. /stopsignal")
    AUTO["running"]=True
    AUTO["task"]=asyncio.create_task(autosignal_loop(ctx, update.effective_chat.id, sym, amount, duration, every, tf))
    await update.message.reply_text("Auto started.")

async def stopsignal_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if int(update.effective_user.id)!=int(ADMIN_ID): return await update.message.reply_text("Not authorized.")
    AUTO["running"]=False; await update.message.reply_text("Stopping...")

# Quick text: "EURUSD-OTC call 5 60"
async def echo_parse(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    parts=update.message.text.strip().split()
    if len(parts)==4 and parts[1].lower() in ("call","put"):
        if int(update.effective_user.id)!=int(ADMIN_ID): return await update.message.reply_text("Not authorized.")
        sym, side, amount, dur = parts[0], parts[1], float(parts[2]), int(parts[3])
        await signal_cmd(update, type("obj", (), {"args":[sym, side, str(amount), str(dur)]}))

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("pologin", pologin))
    app.add_handler(CommandHandler("check", check_cmd))
    app.add_handler(CommandHandler("signal", signal_cmd))
    app.add_handler(CommandHandler("autosignal", autosignal_cmd))
    app.add_handler(CommandHandler("stopsignal", stopsignal_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo_parse))
    app.run_polling(close_loop=False)

if __name__=="__main__":
    main()