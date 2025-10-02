import os, time, threading, requests, json
from datetime import datetime, timedelta
from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler

# === KEYS (as you provided) ===
TELEGRAM_BOT_TOKEN = "8471181182:AAEKGH1UASa5XvkXscb3jb5d1Yz19B8oJNM"
TWELVE_API_KEY     = "9aa4ea677d00474aa0c3223d0c812425"
ALPHA_VANTAGE_KEY  = "BM22MZEIOLL68RI6"

PUBLIC_URL = "https://moneymakerjbsbot.onrender.com"
PORT = int(os.environ.get("PORT", "10000"))

# === STATE ===
STATE = {
    "watchlist": ["EURUSD-OTC", "GBPUSD-OTC", "USDJPY-OTC", "AUDUSD-OTC", "USDCHF-OTC"],
    "autopoll_running": False, "autopoll_thread": None,
    "duration_min": 60,
    "cooldown_min": 5, "min_signal_gap_min": 7,
    "threshold": 0.80, "require_votes": 4, "atr_floor": 0.0006,
    "loss_streak_limit": 3, "loss_streak": 0,
    "min_payout": 80, "require_payout_known": False,  # keep simple: don’t block on payout feed
    "payouts": {},
    "last_signal_time": None, "pair_last_signal_time": {}
}

# === DATA (Twelve primary, AV fallback for non-OTC) ===
def fetch_twelve(symbol, tf="5min"):
    url = "https://api.twelvedata.com/time_series"
    p = {"symbol":symbol,"interval":tf,"outputsize":60,"apikey":TWELVE_API_KEY,"order":"ASC","timezone":"UTC"}
    try:
        r = requests.get(url, params=p, timeout=12).json()
        if "values" not in r: return None
        out=[]
        for v in r["values"]:
            out.append({"t":datetime.fromisoformat(v["datetime"]),
                        "o":float(v["open"]),"h":float(v["high"]),
                        "l":float(v["low"]), "c":float(v["close"])})
        out.sort(key=lambda x:x["t"]); return out[-60:]
    except: return None

def fetch_av(symbol, tf="5min"):
    if "-OTC" in symbol: return None
    try:
        base,quote=symbol[:3],symbol[3:]
        url="https://www.alphavantage.co/query"
        p={"function":"FX_INTRADAY","from_symbol":base,"to_symbol":quote,
           "interval":tf,"apikey":ALPHA_VANTAGE_KEY,"outputsize":"compact"}
        j=requests.get(url,params=p,timeout=12).json()
        key="Time Series FX (5min)"
        if key not in j: return None
        out=[]
        for ts,v in j[key].items():
            out.append({"t":datetime.fromisoformat(ts),
                        "o":float(v["1. open"]),"h":float(v["2. high"]),
                        "l":float(v["3. low"]), "c":float(v["4. close"])})
        out.sort(key=lambda x:x["t"]); return out[-60:]
    except: return None

def fetch_ohlcv(symbol, tf="5min"):
    d=fetch_twelve(symbol,tf)
    return d if d else fetch_av(symbol,tf)

# === indicators (lean) ===
def ema_last(vals, n):
    if len(vals)<n: return None
    k=2/(n+1); e=sum(vals[:n])/n
    for v in vals[n:]: e=e+k*(v-e)
    return e

def rsi_last(vals, n=14):
    if len(vals)<n+1: return None
    gains=[]; losses=[]
    for i in range(1,len(vals)):
        ch=vals[i]-vals[i-1]
        gains.append(max(ch,0)); losses.append(max(-ch,0))
    ag=sum(gains[:n])/n; al=sum(losses[:n])/n
    for i in range(n,len(gains)):
        ag=(ag*(n-1)+gains[i])/n; al=(al*(n-1)+losses[i])/n
    rs=(ag/al) if al!=0 else 999
    return 100-100/(1+rs)

def macd_last(vals, f=12, s=26, sig=9):
    if len(vals)<s+sig: return None,None
    def ema_series(arr,n):
        if len(arr)<n: return []
        k=2/(n+1); e=sum(arr[:n])/n; out=[None]*(n-1)+[e]
        for v in arr[n:]: e=e+k*(v-e); out.append(e)
        return out
    ef=ema_series(vals,f); es=ema_series(vals,s)
    line=[(ef[i]-es[i]) if ef[i] and es[i] else None for i in range(len(vals))]
    m=[x for x in line if x is not None]
    if len(m)<sig: return None,None
    sigs=ema_series(m,sig)
    return line[-1],sigs[-1]

def atr_last(highs,lows,closes,n=14):
    if len(closes)<n+1: return None
    trs=[]
    for i in range(1,len(closes)):
        trs.append(max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])))
    atr=sum(trs[:n])/n
    for i in range(n,len(trs)): atr=(atr*(n-1)+trs[i])/n
    return atr

# === strategy ===
def analyze(symbol):
    bars=fetch_ohlcv(symbol,"5min")
    if not bars or len(bars)<60: return (False,None,0.0,"no data")
    closes=[b["c"] for b in bars]; highs=[b["h"] for b in bars]; lows=[b["l"] for b in bars]
    ema20=ema_last(closes,20); ema50=ema_last(closes,50)
    ema200=ema_last(closes,200) if len(closes)>=200 else sum(closes)/len(closes)
    rsi=rsi_last(closes,14); macd_line,macd_sig=macd_last(closes); atr=atr_last(highs,lows,closes,14)
    if atr is None or atr<STATE["atr_floor"]: return (False,None,0.0,"low atr")
    up=dn=0
    up+=1 if closes[-1]>ema200 else 0; dn+=0 if closes[-1]>ema200 else 1
    if rsi is not None: up+=1 if rsi>50 else 0; dn+=0 if rsi>50 else 1
    if macd_line is not None and macd_sig is not None: up+=1 if macd_line>macd_sig else 0; dn+=0 if macd_line>macd_sig else 1
    if ema20 is not None and ema50 is not None: up+=1 if ema20>ema50 else 0; dn+=0 if ema20>ema50 else 1
    need=STATE["require_votes"]
    if up>=need and up>dn: side,votes="BUY",up
    elif dn>=need and dn>up: side,votes="SELL",dn
    else: return (False,None,0.0,f"no side up={up} dn={dn}")
    conf=max(0.0,min(0.95,0.70+0.05*(votes-4)))
    if conf<STATE["threshold"]: return (False,None,conf,"low conf")
    return (True,side,conf,"ok")

# === Telegram I/O ===
def send_signal(bot, chat_id, pair, side, conf):
    kb=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Win",callback_data="win"),
                              InlineKeyboardButton("❌ Loss",callback_data="loss"),
                              InlineKeyboardButton("⏭ Skip",callback_data="skip")]])
    txt=f"📊 OTC Signal\nPair: {pair}\n👉 {side}\nConfidence: {int(conf*100)}%"
    bot.send_message(chat_id=chat_id, text=txt, reply_markup=kb)

def autopoll_loop(bot, chat_id):
    start=datetime.now(); end=start+timedelta(minutes=STATE["duration_min"])
    while datetime.now()<end and STATE["autopoll_running"]:
        for pair in STATE["watchlist"]:
            if not STATE["autopoll_running"]: break
            now=datetime.now()
            last=STATE["pair_last_signal_time"].get(pair)
            if last and (now-last).total_seconds()<STATE["cooldown_min"]*60: continue
            if STATE["last_signal_time"] and (now-STATE["last_signal_time"]).total_seconds()<STATE["min_signal_gap_min"]*60: continue
            ok,side,conf,_=analyze(pair)
            if ok:
                if STATE["loss_streak"]>=STATE["loss_streak_limit"]:
                    bot.send_message(chat_id,"🚫 3 losses in a row. Stopping."); STATE["autopoll_running"]=False; return
                send_signal(bot,chat_id,pair,side,conf)
                STATE["pair_last_signal_time"][pair]=now; STATE["last_signal_time"]=now
        time.sleep(300)

def on_button(update,ctx):
    q=update.callback_query
    if not q: return
    if q.data=="win": STATE["loss_streak"]=0; q.edit_message_text(q.message.text+"\n✅ WIN")
    elif q.data=="loss": STATE["loss_streak"]+=1; q.edit_message_text(q.message.text+"\n❌ LOSS")
    else: q.edit_message_text(q.message.text+"\n⏭ SKIP")
    q.answer()

def cmd_start(u,c): u.message.reply_text("Ready. Use /otc [min_payout] then /autopoll")
def cmd_otc(u,c):
    try:
        if c.args: STATE["min_payout"]=max(50,min(100,int(c.args[0])))
    except: pass
    u.message.reply_text(f"OTC mode on. Min payout filter set to {STATE['min_payout']}% (not enforced in this lean restore).")
def cmd_autopoll(u,c):
    if STATE["autopoll_running"]: u.message.reply_text("Already running."); return
    STATE["autopoll_running"]=True; STATE["last_signal_time"]=None; STATE["pair_last_signal_time"]={}
    t=threading.Thread(target=autopoll_loop,args=(c.bot,u.effective_chat.id),daemon=True); STATE["autopoll_thread"]=t; t.start()
    u.message.reply_text("▶️ Autopoll started.")
def cmd_stop(u,c): STATE["autopoll_running"]=False; u.message.reply_text("⏹️ Stopped.")

# === MAIN (webhook only; no polling) ===
def main():
    updater=Updater(TELEGRAM_BOT_TOKEN,use_context=True)
    dp=updater.dispatcher
    dp.add_handler(CommandHandler("start",cmd_start))
    dp.add_handler(CommandHandler("otc",cmd_otc))
    dp.add_handler(CommandHandler("autopoll",cmd_autopoll))
    dp.add_handler(CommandHandler("stop",cmd_stop))
    dp.add_handler(CallbackQueryHandler(on_button))
    updater.start_webhook(listen="0.0.0.0",port=PORT,url_path=TELEGRAM_BOT_TOKEN,
                          webhook_url=f"{PUBLIC_URL}/{TELEGRAM_BOT_TOKEN}")
    updater.idle()

if __name__=="__main__": main()