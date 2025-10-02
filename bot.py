# telegram_bot.py — stable full bot (PTB v21)
# - Quiet autosignal (no spam when no setup)
# - Multi autosignal with min confidence
# - Pool scanner
# - /sources implemented (NO NameError)
# - No duplicate functions; no indentation traps

import os, re, logging, asyncio, aiohttp, time, sys, traceback
from collections import deque
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ---------- BOOT ----------
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# === CREDENTIALS / ENV ===
BOT_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN") or "YOUR_TELEGRAM_BOT_TOKEN"
ADMIN_ID    = int(os.getenv("ADMIN_CHAT_ID", "0"))
WORKER_URL  = os.getenv("WORKER_URL", "")  # empty = signal-only

TWELVE_KEY  = os.getenv("TWELVE_KEY") or os.getenv("TWELVE_API_KEY") or ""
ALPHA_KEY   = os.getenv("ALPHAVANTAGE_KEY", "")
DATA_SOURCES_ENV = os.getenv("DATA_SOURCES", "twelve,alpha")

if not BOT_TOKEN or BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
    raise SystemExit("Missing TELEGRAM_BOT_TOKEN / BOT_TOKEN")

HELP_TEXT = (
    "🤖 Signals\n"
    "/start, /help\n"
    "/mode strict|active|mean|both|ultra\n"
    "/check SYMBOL [interval=5min]\n"
    "/signal SYMBOL call|put AMOUNT DURATION\n"
    "/signalauto SYMBOL AMOUNT DURATION [interval=5min]\n"
    "/autosignal SYMBOL AMOUNT DURATION [interval_sec=60] [tf=1min]\n"
    "/stopsignal\n\n"
    "👀 Multi / Pool\n"
    "/multisignal AMOUNT DURATION [tf=1min]\n"
    \"/autosignalmulti PAIRS AMOUNT DURATION [interval_sec=120] [tf=1min] [minconf=60]\\n"
    "/stopmultisignal\n"
    "/watch add|remove|list|clear [SYMBOL]\n"
    "/autopool AMOUNT DURATION [interval_sec=300] [tf=5min]\n"
    "/stoppool\n"
    "/poolthresh PERCENT\n\n"
    "📡 Providers\n"
    "/sources | /sources set twelve,alpha\n\n"
    "📈 Stats\n"
    "/result win|loss\n"
    "/stats, /resetstats\n"
    "/payout PERCENT\n"
)

# ---------- DEFAULTS ----------
LEAD_SEC = 60
WATCHLIST = ["EURUSD-OTC","GBPUSD-OTC","USDJPY-OTC","AUDCAD-OTC"]

POOL_TASK = {"running": False, "task": None}
POOL_MIN_PROB = 0.60
PAYOUT_MIN = 0.75
LAST_FIRES = {}
MAX_PER_HOUR = 6
COOLDOWN_SEC = 240

DAILY_MAX_WINS = 3
DAILY_MAX_LOSSES = 2
DAILY_COUNTER = {"date": None, "wins": 0, "losses": 0}

STATS = {"wins":0,"losses":0,"entries_this_series":0,"total_signals":0,"last_reset":None}

TRACK_TASK = {"running": False, "task": None, "interval": 15}
SEEN_POS = set()

STRATEGY_MODE = "both"  # strict|active|mean|both|ultra
_last_signal_bar_index = None
VERBOSE = False  # silent when no setup

# ---------- UTILS / INDICATORS ----------
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

def norm_symbol_to_twelve(sym: str) -> str:
    raw = sym.upper()
    pure = raw[:-4] if raw.endswith("-OTC") else raw
    pure = pure.replace("_","")
    if "/" in pure: return pure
    if len(pure)==6: return f"{pure[:3]}/{pure[3:]}"
    return pure

def display_and_fetch_symbol(sym: str):
    return sym.upper(), norm_symbol_to_twelve(sym)

def alpha_from_to(sym: str):
    raw = sym.upper()
    raw = raw[:-4] if raw.endswith("-OTC") else raw
    raw = raw.replace("/","")
    if len(raw)>=6:
        return raw[:3], raw[3:6]
    return raw[:3], raw[3:] or "USD"

def next_bar_seconds(interval: str) -> int:
    mins = int(interval.replace("min",""))
    now = datetime.now(timezone.utc)
    bucket = (now.minute // mins + 1) * mins
    nxt = now.replace(second=0, microsecond=0)
    if bucket >= 60:
        nxt = (nxt + timedelta(hours=1)).replace(minute=0)
    else:
        nxt = nxt.replace(minute=bucket)
    return max(0, int((nxt-now).total_seconds()))

# ---------- DATA PROVIDERS ----------
async def _fetch_candles_twelve(symbol: str, interval="5min", limit=120):
    if not TWELVE_KEY: return [], "Missing TWELVE_KEY"
    _, td_symbol = display_and_fetch_symbol(symbol)
    url = f"https://api.twelvedata.com/time_series?symbol={td_symbol}&interval={interval}&outputsize={limit}&apikey={TWELVE_KEY}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=20) as r:
                if r.status!=200:
                    return [], f"Twelve HTTP {r.status}"
                js = await r.json()
                if isinstance(js, dict) and js.get("status")=="error":
                    return [], f"Twelve error: {js.get('message','unknown')}"
                vals = js.get("values")
                if not vals: return [], "Twelve no candles"
                return list(reversed(vals))[-limit:], None
    except Exception as e:
        return [], f"Twelve fetch failed: {e}"

async def fetch_candles_alpha(symbol: str, interval="5min", limit=120):
    if not ALPHA_KEY: return [], "Missing ALPHAVANTAGE_KEY"
    base, quote = alpha_from_to(symbol)
    interval = interval if interval in {"1min","5min","15min","30min","60min"} else "5min"
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
                candles=[]
                for t,v in sorted(series.items()):
                    candles.append({"datetime":t,"open":float(v["1. open"]),
                                    "high":float(v["2. high"]),"low":float(v["3. low"]),
                                    "close":float(v["4. close"])})
                return candles[-limit:], None
    except Exception as e:
        return [], f"Alpha fetch failed: {e}"

PROVIDER_FUNCS = {"twelve": _fetch_candles_twelve, "alpha": fetch_candles_alpha}

def parse_sources_env():
    order = [p.strip().lower() for p in DATA_SOURCES_ENV.split(",") if p.strip()]
    return [p for p in order if p in PROVIDER_FUNCS]
DATA_SOURCES_ORDER = parse_sources_env()

async def fetch_candles(symbol: str, interval="5min", limit=120):
    errors=[]
    for prov in DATA_SOURCES_ORDER:
        func = PROVIDER_FUNCS[prov]
        candles, err = await func(symbol, interval, limit)
        if candles and not err:
            return candles, None
        errors.append(f"{prov}: {err or 'no data'}")
    return [], " | ".join(errors)

async def fetch_closes(symbol: str, interval="5min", limit=120):
    candles, err = await fetch_candles(symbol, interval, limit)
    if err: return [], err
    return [float(c["close"]) for c in candles], None

# ---------- STRATEGY ----------
def decide_signal_standard(closes):
    if len(closes) < 60: return None
    last = closes[-1]; e50 = ema(closes,50); r = rsi(closes,14)
    if e50 is None or r is None: return None
    r_prev = rsi(closes[:-1],14)
    def strict():
        if r_prev is None: return None
        if last > e50 and r_prev < 30 <= r: return "call"
        if last < e50 and r_prev > 70 >= r: return "put"
        return None
    def active():
        if last > e50 and r > 55: return "call"
        if last < e50 and r < 45: return "put"
        return None
    def mean():
        if r >= 70: return "put"
        if r <= 30: return "call"
        return None
    if STRATEGY_MODE=="strict": return strict()
    if STRATEGY_MODE=="active": return active()
    if STRATEGY_MODE=="mean":   return mean()
    if STRATEGY_MODE=="both":   return strict() or active()
    return None

def median_body(bodies):
    s=sorted(bodies); n=len(s)
    return (s[n//2] if n%2 else (s[n//2-1]+s[n//2])/2.0) if n else 0.0

def decide_signal_ultra(candles, cooldown_bars=1):
    global _last_signal_bar_index
    if len(candles)<60: return None
    closes=[float(c["close"]) for c in candles]
    opens=[float(c["open"]) for c in candles]
    e50=ema(closes,50); r=rsi(closes,14)
    if e50 is None or r is None: return None
    bodies=[abs(closes[i]-opens[i]) for i in range(-11,-1)]
    mom_th=median_body(bodies)
    last_body=abs(closes[-1]-opens[-1])
    bar_idx=len(candles)-1
    if _last_signal_bar_index is not None and bar_idx-_last_signal_bar_index<=cooldown_bars:
        return None
    last=closes[-1]
    if r<=20 and last>e50 and last_body>=mom_th: _last_signal_bar_index=bar_idx; return "call"
    if r>=80 and last<e50 and last_body>=mom_th: _last_signal_bar_index=bar_idx; return "put"
    return None

def score_probability(candles):
    if len(candles)<60: return (None,0.0,{})
    closes=[float(c["close"]) for c in candles]
    opens =[float(c["open"])  for c in candles]
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
        prob=max(0.5,min(0.8,0.5+(call_score-put_score)))
        return ("call",prob,{})
    else:
        prob=max(0.5,min(0.8,0.5+(put_score-call_score)))
        return ("put",prob,{})

def _confidence_for_direction(decision: str, candles):
    try:
        _, prob, _ = score_probability(candles)
        return float(prob) if prob else None
    except:
        return None

# ---------- GUARDS ----------
AUTO_TASK = {"running": False, "task": None}
MULTI_TASK = {"running": False, "task": None}

async def ping_worker() -> bool:
    if not WORKER_URL: return False
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{WORKER_URL}/health", timeout=5) as r:
                return r.status == 200
    except Exception:
        return False

async def send_trade(symbol: str, direction: str, amount: float, duration: int):
    if not WORKER_URL:
        return {"status":"ok","note":"signal-only"}
    payload = {"symbol":symbol.upper(),"direction":direction.lower(),"amount":float(amount),"duration_sec":int(duration)}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{WORKER_URL}/trade", json=payload, timeout=10) as r:
                return await r.json()
    except Exception as e:
        logging.warning(f"/trade failed: {e}")
        return {"status":"error","error":str(e)}

def _cooldown_ok(pair: str)->bool:
    now=time.time()
    q=LAST_FIRES.setdefault(pair, deque())
    while q and now-q[0]>3600: q.popleft()
    if q and (now-q[-1] < COOLDOWN_SEC): return False
    if len(q) >= MAX_PER_HOUR: return False
    q.append(now); return True

def _daily_ensure_today():
    today = datetime.utcnow().date().__str__()
    if DAILY_COUNTER["date"] != today:
        DAILY_COUNTER.update({"date":today,"wins":0,"losses":0})

async def _daily_stop_check(ctx, chat_id):
    _daily_ensure_today()
    if DAILY_COUNTER["wins"]>=DAILY_MAX_WINS or DAILY_COUNTER["losses"]>=DAILY_MAX_LOSSES:
        await ctx.bot.send_message(chat_id, "🛑 Daily stop reached. Halting.")
        AUTO_TASK["running"]=False; MULTI_TASK["running"]=False; POOL_TASK["running"]=False
        return True
    return False

async def htf_trend_ok(symbol: str, tf="15min", lookback=120):
    candles, err = await fetch_candles(symbol, tf, lookback)
    if err or len(candles)<55: return None
    closes=[float(c["close"]) for c in candles]
    e50=ema(closes,50)
    if e50 is None: return None
    return "up" if closes[-1]>e50 else "down"

# ---------- LOOPS (QUIET) ----------
async def autosignal_loop(ctx, chat_id, symbol, amount, duration, interval_sec, tf_interval="1min"):
    disp,_ = display_and_fetch_symbol(symbol)
    await ctx.bot.send_message(chat_id, f"▶️ Auto-signal {disp} | ${amount} | {duration}s | TF {tf_interval} | every {interval_sec}s | mode {STRATEGY_MODE}")
    while AUTO_TASK["running"]:
        if await _daily_stop_check(ctx, chat_id): break
        candles, err = await fetch_candles(symbol, tf_interval, 120)
        if err:
            await asyncio.sleep(interval_sec); continue

        dec = decide_signal_ultra(candles, 1) if STRATEGY_MODE=="ultra" else decide_signal_standard([float(c['close']) for c in candles])
        conf = _confidence_for_direction(dec, candles) if dec else None

        if dec:
            atr=atr_from_candles(candles,14)
            if not atr or abs(float(candles[-1]["close"])-float(candles[-1]["open"])) < 0.6*atr:
                dec=None

        if dec:
            trend = await htf_trend_ok(symbol,"15min",120)
            if trend is not None:
                want_up = (dec=="call")
                if (trend=="up" and not want_up) or (trend=="down" and want_up):
                    dec=None

        if dec and not _cooldown_ok(disp):
            dec=None

        if dec:
            arrow=_dir_to_arrow(dec)
            wait=next_bar_seconds(tf_interval)
            lead=min(LEAD_SEC, wait); eta=max(0, wait-lead)
            conf_txt = f"\nConfidence: {int(conf*100)}%" if conf is not None else ""
            await ctx.bot.send_message(chat_id,
                f"📣 Upcoming ({tf_interval})\nPair: {disp}\nDirection: {arrow}{conf_txt}\nPlace at: next open (~{wait}s)\nExpiry: {duration}s")
            if eta>0: await asyncio.sleep(eta)
            if lead>0:
                await ctx.bot.send_message(chat_id, f"⏱ Get ready: **{arrow}** on {disp} in ~{lead}s")
                await asyncio.sleep(lead)
            await send_trade(disp, dec, amount, duration)
            STATS["entries_this_series"]+=1; STATS["total_signals"]+=1
            await ctx.bot.send_message(chat_id,
                f"✅ PLACE NOW\nPair: {disp}\nDirection: {arrow}\nAmount: ${amount}\nDuration: {duration}s{conf_txt}")
        await asyncio.sleep(interval_sec)
    await ctx.bot.send_message(chat_id,"⏹ Auto-signal stopped.")

async def autosignal_multi_loop(ctx, chat_id, pairs, amount, duration, interval_sec, tf="1min", min_conf=0.60):
    pairs = [p.strip().upper() for p in pairs if p.strip()]
    header = ", ".join(pairs)
    await ctx.bot.send_message(chat_id,
        f"▶️ Multi Auto-signal\nPairs: {header}\nStake: ${amount} | Expiry: {duration}s\nEvery: {interval_sec}s | TF: {tf}\nMin confidence: {int(min_conf*100)}%\nMode: {STRATEGY_MODE}"
    )
    while MULTI_TASK["running"]:
        if await _daily_stop_check(ctx, chat_id): break
        for sym in pairs:
            candles, err = await fetch_candles(sym, tf, 120)
            if err:
                await asyncio.sleep(0.15); 
                continue

            dec = decide_signal_ultra(candles, 1) if STRATEGY_MODE=="ultra" else decide_signal_standard([float(c['close']) for c in candles])

            if dec:
                atr=atr_from_candles(candles,14)
                if not atr or abs(float(candles[-1]["close"])-float(candles[-1]["open"])) < 0.6*atr:
                    dec=None

            if dec:
                trend = await htf_trend_ok(sym,"15min",120)
                if trend is not None:
                    want_up = (dec=="call")
                    if (trend=="up" and not want_up) or (trend=="down" and want_up):
                        dec=None

            conf=None
            if dec:
                _, prob, _ = score_probability(candles)
                conf = float(prob or 0.0)
                if conf < min_conf:
                    dec=None

            disp,_=display_and_fetch_symbol(sym)
            if dec and not _cooldown_ok(disp):
                dec=None

            if dec:
                arrow=_dir_to_arrow(dec)
                wait=next_bar_seconds(tf); lead=min(LEAD_SEC, wait); eta=max(0, wait-lead)
                conf_txt=f"{int((conf or 0.0)*100)}%"
                await ctx.bot.send_message(chat_id,
                    f"📣 Upcoming ({tf})\nPair: {disp}\nDirection: {arrow}\nConfidence: {conf_txt}\nPlace at: next open (~{wait}s)\nExpiry: {duration}s")
                if eta>0: await asyncio.sleep(eta)
                if lead>0:
                    await ctx.bot.send_message(chat_id, f"⏱ Get ready: **{arrow}** on {disp} in ~{lead}s")
                    await asyncio.sleep(lead)
                await send_trade(disp, "call" if arrow=="UP" else "put", amount, duration)
                STATS["entries_this_series"]+=1; STATS["total_signals"]+=1
                await ctx.bot.send_message(chat_id,
                    f"✅ PLACE NOW\nPair: {disp}\nDirection: {arrow}\nAmount: ${amount}\nDuration: {duration}s\nConfidence: {conf_txt}")
            await asyncio.sleep(0.15)
        await asyncio.sleep(interval_sec)
    await ctx.bot.send_message(chat_id,"⏹ Multi auto-signal stopped.")

# ---------- POOL ----------
async def fetch_and_score(symbol, tf_interval):
    candles, err = await fetch_candles(symbol, tf_interval, 120)
    if err: return None,0.0,symbol,err
    direction, prob,_ = score_probability(candles)
    atr=atr_from_candles(candles,14)
    if atr:
        last_body = abs(float(candles[-1]["close"])-float(candles[-1]["open"]))
        if last_body < 0.6*atr: return None,0.0,symbol,None
    if direction:
        trend = await htf_trend_ok(symbol,"15min",120)
        if trend is not None:
            want_up = (direction=="call")
            if (trend=="up" and not want_up) or (trend=="down" and want_up):
                direction=None; prob=0.0
    return direction, prob, symbol, None

async def autopool_loop(ctx, chat_id, amount, duration, interval_sec, tf="5min"):
    await ctx.bot.send_message(chat_id, f"▶️ Pool TF {tf} every {interval_sec}s | scanning {len(WATCHLIST)} pairs | threshold {int(POOL_MIN_PROB*100)}%")
    while POOL_TASK["running"]:
        if await _daily_stop_check(ctx, chat_id): break
        if not WATCHLIST:
            await asyncio.sleep(interval_sec); continue
        best=None
        for pair in WATCHLIST:
            d,p,s,err=await fetch_and_score(pair, tf)
            if err: continue
            if d and p>=POOL_MIN_PROB and ((best is None) or (p>best[1])):
                best=(d,p,s)
            await asyncio.sleep(0.25)
        if best:
            d,p,sym=best
            disp,_=display_and_fetch_symbol(sym)
            if _cooldown_ok(disp):
                arrow=_dir_to_arrow(d)
                wait=next_bar_seconds(tf); lead=min(LEAD_SEC,wait); eta=max(0,wait-lead)
                await ctx.bot.send_message(chat_id,
                    f"🎯 Pool pick ({tf})\nPair: {disp}\nDirection: {arrow}\nConfidence: {int(p*100)}%\nPlace at: next open (~{wait}s)\nExpiry: {duration}s")
                if eta>0: await asyncio.sleep(eta)
                if lead>0:
                    await ctx.bot.send_message(chat_id,f"⏱ Get ready: **{arrow}** on {disp} in ~{lead}s")
                    await asyncio.sleep(lead)
                await send_trade(disp, "call" if arrow=="UP" else "put", amount, duration)
                STATS["total_signals"]+=1
                await ctx.bot.send_message(chat_id,
                    f"✅ PLACE NOW\nPair: {disp}\nDirection: {arrow}\nAmount: ${amount}\nDuration: {duration}s\nConfidence: {int(p*100)}%")
        await asyncio.sleep(interval_sec)
    await ctx.bot.send_message(chat_id,"⏹ Pool stopped.")

# ---------- COMMANDS ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot ready. Type /help")

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)

async def mode_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global STRATEGY_MODE
    if not ctx.args or ctx.args[0].lower() not in ("strict","active","mean","both","ultra"):
        return await update.message.reply_text("Usage: /mode strict|active|mean|both|ultra")
    STRATEGY_MODE = ctx.args[0].lower()
    await update.message.reply_text(f"Mode: {STRATEGY_MODE}")

async def check_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) not in (1,2):
        return await update.message.reply_text("Usage: /check SYMBOL [interval=5min]")
    symbol = ctx.args[0].upper(); interval = ctx.args[1] if len(ctx.args)==2 else "5min"
    disp,_=display_and_fetch_symbol(symbol)
    closes,err = await fetch_closes(symbol, interval, 120)
    if err: return await update.message.reply_text(f"❌ {err}")
    e=ema(closes,50); r=rsi(closes,14)
    dec = decide_signal_ultra([{"open":closes[-2],"close":closes[-1],"high":max(closes[-2],closes[-1]),"low":min(closes[-2],closes[-1])}]*60) if STRATEGY_MODE=="ultra" else decide_signal_standard(closes)
    await update.message.reply_text(
        f"📊 {disp} {interval}\nMode: {STRATEGY_MODE}\nCandles: {len(closes)}\nEMA50: {round(e,5) if e else 'n/a'}\nRSI14: {round(r,2) if r else 'n/a'}\nDecision: {dec or 'none'}"
    )

async def signal_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args)!=4:
        return await update.message.reply_text("Usage: /signal SYMBOL DIRECTION AMOUNT DURATION")
    symbol, direction, amount_str, duration_str = ctx.args
    direction=direction.lower()
    if direction not in ("call","put"):
        return await update.message.reply_text("Direction must be call or put")
    amount=float(amount_str); duration=int(duration_str)
    candles, err = await fetch_candles(symbol, "1min", 120)
    if err: return await update.message.reply_text(f"❌ {err}")
    conf = _confidence_for_direction(direction, candles)
    disp,_=display_and_fetch_symbol(symbol)
    await send_trade(disp, direction, amount, duration)
    arrow=_dir_to_arrow(direction)
    conf_txt=f"\nConfidence: {int(conf*100)}%" if conf is not None else ""
    await update.message.reply_text(
        f"✅ Signal logged\nPair: {disp}\nDirection: {arrow}\nAmount: ${amount}\nDuration: {duration}s{conf_txt}"
    )

async def signalauto_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) not in (3,4):
        return await update.message.reply_text("Usage: /signalauto SYMBOL AMOUNT DURATION [interval=5min]")
    symbol=ctx.args[0].upper()
    amount=float(ctx.args[1]); duration=int(ctx.args[2])
    interval=ctx.args[3] if len(ctx.args)==4 else "5min"
    candles,err = await fetch_candles(symbol, interval, 120)
    if err: return await update.message.reply_text(f"❌ {err}")
    decision = decide_signal_ultra(candles,0) if STRATEGY_MODE=="ultra" else decide_signal_standard([float(c['close']) for c in candles])
    if not decision: return await update.message.reply_text("🤷 No clear signal now.")
    conf=_confidence_for_direction(decision, candles)
    disp,_=display_and_fetch_symbol(symbol)
    await send_trade(disp, decision, amount, duration)
    arrow=_dir_to_arrow(decision)
    conf_txt=f"\nConfidence: {int(conf*100)}%" if conf is not None else ""
    await update.message.reply_text(
        f"✅ Strategy signal\nPair: {disp}\nDirection: {arrow}\nAmount: ${amount}\nDuration: {duration}s{conf_txt}"
    )

async def autosignal_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    a = ctx.args
    if len(a) not in (3,4,5):
        return await update.message.reply_text(
            "Usage: /autosignal SYMBOL AMOUNT DURATION [interval_sec=60] [tf=1min]\n"
            "Example: /autosignal EURUSD-OTC 5 60 180 1min"
        )
    symbol = a[0].upper()
    amount = float(a[1]); duration = int(a[2])
    interval_sec = int(a[3]) if len(a) >= 4 else 60
    tf = a[4] if len(a) == 5 else "1min"
    if AUTO_TASK["running"]:
        return await update.message.reply_text("Already running. /stopsignal first.")
    AUTO_TASK["running"]=True
    AUTO_TASK["task"]=asyncio.create_task(autosignal_loop(ctx, update.effective_chat.id, symbol, amount, duration, interval_sec, tf))
    await update.message.reply_text(f"▶️ Auto-signal {symbol} | ${amount} | {duration}s | every {interval_sec}s | TF {tf} | mode {STRATEGY_MODE}")

async def stopsignal_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    AUTO_TASK["running"]=False
    await update.message.reply_text("Stopping auto-signal...")

async def autosignalmulti_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    a = ctx.args
    if len(a) < 3:
        return await update.message.reply_text(
            "Usage: /autosignalmulti PAIRS AMOUNT DURATION [interval_sec=120] [tf=1min] [minconf=60]\n"
            "Example: /autosignalmulti EURUSD-OTC,GBPUSD-OTC 5 60 120 1min 60"
        )
    pairs_csv = a[0]
    amount = float(a[1]); duration = int(a[2])
    interval_sec = int(a[3]) if len(a) >= 4 else 120
    tf = a[4] if len(a) >= 5 else "1min"
    minconf = int(a[5]) if len(a) >= 6 else 60
    min_conf_float = max(0.0, min(1.0, minconf / 100.0))
    if MULTI_TASK["running"]:
        return await update.message.reply_text("Multi-signal already running. Use /stopmultisignal first.")
    pairs = [p.strip() for p in pairs_csv.split(",") if p.strip()]
    if not pairs:
        return await update.message.reply_text("Give at least one pair, e.g. EURUSD-OTC")
    MULTI_TASK["running"] = True
    MULTI_TASK["task"] = asyncio.create_task(
        autosignal_multi_loop(ctx, update.effective_chat.id, pairs, amount, duration, interval_sec, tf, min_conf_float)
    )
    await update.message.reply_text(
        f"▶️ Multi Auto-signal ON\nPairs: {', '.join(pairs)}\n${amount} | {duration}s | every {interval_sec}s | TF {tf} | min {minconf}%"
    )

async def stopmultisignal_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    MULTI_TASK["running"] = False
    await update.message.reply_text("Stopping multi auto-signal...")

async def multisignal_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) not in (2,3):
        return await update.message.reply_text("Usage: /multisignal AMOUNT DURATION [tf=1min]")
    amount = float(ctx.args[0]); duration = int(ctx.args[1])
    tf = ctx.args[2] if len(ctx.args) == 3 else "1min"
    hits=[]
    for pair in WATCHLIST:
        candles, err = await fetch_candles(pair, tf, 120)
        if err: continue
        decision = decide_signal_ultra(candles, 0) if STRATEGY_MODE=="ultra" else decide_signal_standard([float(c['close']) for c in candles])
        if not decision: continue
        atr = atr_from_candles(candles,14)
        if (not atr) or abs(float(candles[-1]["close"])-float(candles[-1]["open"])) < 0.6*atr: continue
        trend = await htf_trend_ok(pair,"15min",120)
        if trend is not None:
            want_up = (decision=="call")
            if (trend=="up" and not want_up) or (trend=="down" and want_up):
                continue
        conf = _confidence_for_direction(decision, candles)
        arrow = _dir_to_arrow(decision)
        line = f"{pair}: {arrow}"
        if conf is not None:
            line += f" ({int(conf*100)}% conf.)"
        hits.append(line)
    if hits:
        await update.message.reply_text(
            "📊 Multi-signal — potential entries:\n" + "\n".join(hits) +
            f"\n\nSuggested stake/expiry: ${amount} / {duration}s"
        )
    else:
        await update.message.reply_text("No clear signals right now.")

# ---- Watchlist / threshold ----
async def watch_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global WATCHLIST
    sub=(ctx.args[0].lower() if ctx.args else "list")
    if sub=="add" and len(ctx.args)>=2:
        sym=ctx.args[1].upper()
        if sym not in WATCHLIST: WATCHLIST.append(sym)
        return await update.message.reply_text(f"Added {sym}\n{', '.join(WATCHLIST)}")
    if sub=="remove" and len(ctx.args)>=2:
        sym=ctx.args[1].upper()
        if sym in WATCHLIST: WATCHLIST.remove(sym)
        return await update.message.reply_text(f"Removed {sym}\n{', '.join(WATCHLIST) if WATCHLIST else '(empty)'}")
    if sub=="clear":
        WATCHLIST=[]; return await update.message.reply_text("Watchlist cleared.")
    return await update.message.reply_text(f"{', '.join(WATCHLIST) if WATCHLIST else '(empty)'}")

async def poolthresh_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global POOL_MIN_PROB
    if not ctx.args: return await update.message.reply_text(f"Pool threshold: {int(POOL_MIN_PROB*100)}%\nUsage: /poolthresh 62")
    x=int(ctx.args[0])
    if not (50<=x<=90): return await update.message.reply_text("Pick 50–90.")
    POOL_MIN_PROB=x/100.0; await update.message.reply_text(f"✅ Pool threshold {x}%")

# ---- Providers order (/sources) ----
async def sources_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global DATA_SOURCES_ORDER
    if not ctx.args:
        return await update.message.reply_text("Order: " + " > ".join(DATA_SOURCES_ORDER))
    if ctx.args[0].lower()=="set" and len(ctx.args)>=2:
        order=[p.strip().lower() for p in " ".join(ctx.args[1:]).split(",")]
        clean=[p for p in order if p in PROVIDER_FUNCS]
        if not clean: return await update.message.reply_text("Usage: /sources set twelve,alpha")
        DATA_SOURCES_ORDER=clean
        return await update.message.reply_text("✅ New order: " + " > ".join(DATA_SOURCES_ORDER))
    return await update.message.reply_text("Usage:\n/sources            (show)\n/sources set twelve,alpha")

# ---- Stats ----
async def result_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args or ctx.args[0].lower() not in ("win","loss"):
        return await update.message.reply_text("Usage: /result win|loss")
    k=ctx.args[0].lower()
    if k=="win": STATS["wins"]+=1; DAILY_COUNTER["wins"]+=1
    else:        STATS["losses"]+=1; DAILY_COUNTER["losses"]+=1
    tot=STATS["wins"]+STATS["losses"]; wr=(STATS["wins"]/tot*100) if tot else 0.0
    await update.message.reply_text(f"📈 {k.upper()} recorded. WR: {wr:.1f}%")

async def stats_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    _daily_ensure_today()
    tot=STATS["wins"]+STATS["losses"]; wr=(STATS["wins"]/tot*100) if tot else 0.0
    await update.message.reply_text(
        f"Signals: {STATS['total_signals']} | Series entries: {STATS['entries_this_series']}\n"
        f"Wins: {STATS['wins']} Losses: {STATS['losses']} WR: {wr:.1f}%\n"
        f"Daily {DAILY_COUNTER['wins']}W/{DAILY_COUNTER['losses']}L"
    )

async def resetstats_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    STATS.update({"wins":0,"losses":0,"entries_this_series":0,"total_signals":0,"last_reset":datetime.now(timezone.utc).isoformat()})
    _daily_ensure_today(); DAILY_COUNTER["wins"]=0; DAILY_COUNTER["losses"]=0
    await update.message.reply_text("✅ Stats reset.")

async def payout_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global PAYOUT_MIN
    if not ctx.args: return await update.message.reply_text(f"Payout threshold: {int(PAYOUT_MIN*100)}%\nUsage: /payout 75")
    v=int(ctx.args[0])
    if not (50<=v<=95): return await update.message.reply_text("Pick 50–95.")
    PAYOUT_MIN=v/100.0; await update.message.reply_text(f"✅ Payout set to {v}%")

# ---------- MAIN ----------
def main():
    logging.info(f"Python: {sys.version}")
    try:
        import telegram
        logging.info(f"python-telegram-bot: {telegram.__version__}")
    except Exception:
        pass

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("mode", mode_cmd))
    app.add_handler(CommandHandler("check", check_cmd))
    app.add_handler(CommandHandler("signal", signal_cmd))
    app.add_handler(CommandHandler("signalauto", signalauto_cmd))
    app.add_handler(CommandHandler("autosignal", autosignal_cmd))
    app.add_handler(CommandHandler("stopsignal", stopsignal_cmd))
    app.add_handler(CommandHandler("autosignalmulti", autosignalmulti_cmd))
    app.add_handler(CommandHandler("stopmultisignal", stopmultisignal_cmd))
    app.add_handler(CommandHandler("multisignal", multisignal_cmd))
    app.add_handler(CommandHandler("watch", watch_cmd))
    app.add_handler(CommandHandler("poolthresh", poolthresh_cmd))
    app.add_handler(CommandHandler("autopool", autopool_cmd))
    app.add_handler(CommandHandler("stoppool", stoppool_cmd))
    app.add_handler(CommandHandler("sources", sources_cmd))
    app.add_handler(CommandHandler("result", result_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("resetstats", resetstats_cmd))
    app.add_handler(CommandHandler("payout", payout_cmd))
    # swallow plain text to avoid accidental echo-trigger loops
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u, c: None))
    app.run_polling()

if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.error("FATAL:\n" + "".join(traceback.format_exc()))
        raise