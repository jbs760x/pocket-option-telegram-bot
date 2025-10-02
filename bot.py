import os
import time
import json
import threading
import traceback
import requests
from datetime import datetime, timedelta
from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler

# ========= HARD-CODED KEYS (per your request) =========
TELEGRAM_BOT_TOKEN = "8471181182:AAEKGH1UASa5XvkXscb3jb5d1Yz19B8oJNM"
TWELVE_API_KEY     = "9aa4ea677d00474aa0c3223d0c812425"
ALPHA_VANTAGE_KEY  = "BM22MZEIOLL68RI6"

# Your Render URL (no trailing slash)
PUBLIC_URL = "https://moneymakerjbsbot.onrender.com"
PORT = int(os.environ.get("PORT", "10000"))

# ========= POCKET OPTION WEBSOCKET (GUEST MODE) =========
# If this ever changes, set PO_WS_URL in Render Environment to override.
PO_WS_URL = os.environ.get("PO_WS_URL", "wss://ws.pocketoption.net/")

# ========= BOT STATE (LEAN + STRICT OTC) =========
STATE = {
    # Watch exactly 5 OTC pairs (edit if you need different ones)
    "watchlist": ["EURUSD-OTC", "GBPUSD-OTC", "USDJPY-OTC", "AUDUSD-OTC", "USDCHF-OTC"],

    "autopoll_running": False,
    "autopoll_thread": None,

    # Session & cadence
    "duration_min": 60,           # how long /autopoll runs
    "cooldown_min": 5,            # per-pair cooldown (minutes)
    "min_signal_gap_min": 7,      # global min gap between any two signals (minutes)

    # Accuracy filters (strict)
    "threshold": 0.80,            # confidence threshold (80%)
    "require_votes": 4,           # must hit all 4: Trend, RSI50, MACD>Sig, EMA20>EMA50
    "atr_floor": 0.0006,          # skip flat/choppy markets

    # Risk guardrail
    "loss_streak_limit": 3,
    "loss_streak": 0,

    # Payout filtering (guest WS)
    "min_payout": 80,             # require payout ≥ this %
    "require_payout_known": True, # if True, skip if payout unknown from WS
    "payouts": {},                # filled by WS listener, e.g. {"EURUSD-OTC": 82}

    # Internals
    "last_signal_time": None,
    "pair_last_signal_time": {}
}

# ========= DATA FETCHERS (Twelve primary, AV fallback for non-OTC) =========
def fetch_twelve(symbol, tf="5min"):
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol, "interval": tf, "outputsize": 60,
        "apikey": TWELVE_API_KEY, "order": "ASC", "timezone": "UTC"
    }
    try:
        r = requests.get(url, params=params, timeout=12)
        r.raise_for_status()
        j = r.json()
        if "values" not in j:
            return None
        out = []
        for v in j["values"]:
            out.append({
                "t": datetime.fromisoformat(v["datetime"]),
                "o": float(v["open"]), "h": float(v["high"]),
                "l": float(v["low"]),  "c": float(v["close"])
            })
        out.sort(key=lambda x: x["t"])
        return out[-60:]
    except Exception:
        return None

def fetch_av(symbol, tf="5min"):
    # Alpha Vantage doesn't support OTC; fallback only for non-OTC pairs
    if "-OTC" in symbol:
        return None
    try:
        base, quote = symbol[:3], symbol[3:]
        url = "https://www.alphavantage.co/query"
        params = {
            "function": "FX_INTRADAY", "from_symbol": base, "to_symbol": quote,
            "interval": tf, "apikey": ALPHA_VANTAGE_KEY, "outputsize": "compact"
        }
        j = requests.get(url, params=params, timeout=12).json()
        key = "Time Series FX (5min)"
        if key not in j:
            return None
        items = []
        for ts, v in j[key].items():
            items.append({
                "t": datetime.fromisoformat(ts),
                "o": float(v["1. open"]), "h": float(v["2. high"]),
                "l": float(v["3. low"]),  "c": float(v["4. close"])
            })
        items.sort(key=lambda x: x["t"])
        return items[-60:]
    except Exception:
        return None

def fetch_ohlcv(symbol, tf="5min"):
    bars = fetch_twelve(symbol, tf)
    if bars:
        return bars
    return fetch_av(symbol, tf)

# ========= INDICATORS (lean but solid) =========
def ema_last(vals, period):
    if len(vals) < period:
        return None
    k = 2/(period+1)
    e = sum(vals[:period]) / period
    for v in vals[period:]:
        e = e + k*(v - e)
    return e

def rsi_last(vals, period=14):
    if len(vals) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(vals)):
        ch = vals[i] - vals[i-1]
        gains.append(max(ch, 0))
        losses.append(max(-ch, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain*(period-1) + gains[i]) / period
        avg_loss = (avg_loss*(period-1) + losses[i]) / period
    rs = (avg_gain / avg_loss) if avg_loss != 0 else 999
    return 100 - 100/(1+rs)

def macd_last(vals, fast=12, slow=26, signal=9):
    if len(vals) < slow + signal:
        return None, None
    # compute EMA series cheaply
    def ema_series(arr, n):
        if len(arr) < n: return []
        k = 2/(n+1)
        e = sum(arr[:n]) / n
        out = [None]*(n-1) + [e]
        for v in arr[n:]:
            e = e + k*(v - e)
            out.append(e)
        return out
    ef = ema_series(vals, fast)
    es = ema_series(vals, slow)
    macd_line = [ (ef[i] - es[i]) if ef[i] and es[i] else None for i in range(len(vals)) ]
    macd_vals = [x for x in macd_line if x is not None]
    if len(macd_vals) < signal:
        return None, None
    sig_series = ema_series(macd_vals, signal)
    return macd_line[-1], sig_series[-1]

def atr_last(highs, lows, closes, period=14):
    if len(closes) < period + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])))
    atr = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        atr = (atr*(period-1) + trs[i]) / period
    return atr

# ========= STRATEGY (strict OTC confluence) =========
def analyze(symbol):
    bars = fetch_ohlcv(symbol, "5min")
    if not bars or len(bars) < 60:
        return (False, None, 0.0, "No/low data")

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
        return (False, None, 0.0, "Low ATR")

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
        side, votes = "BUY", up
    elif dn >= need and dn > up:
        side, votes = "SELL", dn
    else:
        return (False, None, 0.0, f"No side (up={up} dn={dn})")

    # Confidence derived from votes (strict baseline, cap 95%)
    conf = max(0.0, min(0.95, 0.70 + 0.05*(votes - 4)))  # 4 votes -> 70%, 5 -> 75%, etc.
    if conf < STATE["threshold"]:
        return (False, None, conf, "Low conf")

    # Payout filter (guest WS or manual)
    payout = STATE["payouts"].get(symbol)
    if payout is None and STATE["require_payout_known"]:
        return (False, None, conf, "Payout unknown")
    if payout is not None and payout < STATE["min_payout"]:
        return (False, None, conf, f"Payout {payout}% < {STATE['min_payout']}%")

    return (True, side, conf, f"votes={votes} atr={atr14:.5f} payout={payout if payout is not None else 'n/a'}")

# ========= TELEGRAM SIGNALS =========
def send_signal(bot, chat_id, pair, side, conf):
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Win", callback_data="win"),
        InlineKeyboardButton("❌ Loss", callback_data="loss"),
        InlineKeyboardButton("⏭ Skip", callback_data="skip")
    ]])
    payout_txt = STATE["payouts"].get(pair)
    if payout_txt is None:
        payout_line = f"Min Payout Required: {STATE['min_payout']}% (payout unknown)"
    else:
        payout_line = f"Payout: {payout_txt}% (min {STATE['min_payout']}%)"

    txt = (
        "📊 OTC Signal\n"
        f"Pair: {pair}\n"
        f"👉 {side}\n"
        f"Confidence: {int(conf*100)}%\n"
        f"{payout_line}"
    )
    bot.send_message(chat_id=chat_id, text=txt, reply_markup=keyboard)

# ========= AUTOPOLL LOOP =========
def autopoll_loop(bot, chat_id):
    start = datetime.now()
    end_time = start + timedelta(minutes=STATE["duration_min"])
    while datetime.now() < end_time and STATE["autopoll_running"]:
        for pair in STATE["watchlist"]:
            if not STATE["autopoll_running"]:
                break

            now = datetime.now()
            # per-pair cooldown
            last_pair = STATE["pair_last_signal_time"].get(pair)
            if last_pair and (now - last_pair).total_seconds() < STATE["cooldown_min"]*60:
                continue
            # global min gap between any two signals
            if STATE["last_signal_time"] and (now - STATE["last_signal_time"]).total_seconds() < STATE["min_signal_gap_min"]*60:
                continue

            should, side, conf, why = analyze(pair)
            if should:
                if STATE["loss_streak"] >= STATE["loss_streak_limit"]:
                    bot.send_message(chat_id, "🚫 3 losses in a row. Stopping.")
                    STATE["autopoll_running"] = False
                    return
                send_signal(bot, chat_id, pair, side, conf)
                STATE["pair_last_signal_time"][pair] = now
                STATE["last_signal_time"] = now

        # exactly one scan per candle close (5min)
        time.sleep(300)

# ========= INLINE BUTTONS =========
def on_button(update, ctx):
    q = update.callback_query
    if not q:
        return
    if q.data == "win":
        STATE["loss_streak"] = 0
        q.edit_message_text(q.message.text + "\n✅ WIN")
    elif q.data == "loss":
        STATE["loss_streak"] += 1
        q.edit_message_text(q.message.text + "\n❌ LOSS")
    else:
        q.edit_message_text(q.message.text + "\n⏭ SKIP")
    q.answer()

# ========= COMMANDS (lean) =========
def cmd_start(update, ctx):
    update.message.reply_text(
        "Bot ready ✅\n"
        "Use /otc [min_payout] then /autopoll\n"
        "Example: /otc 80  → only signal if payout ≥ 80%\n"
        "Default pairs (5 OTC): EURUSD-OTC, GBPUSD-OTC, USDJPY-OTC, AUDUSD-OTC, USDCHF-OTC"
    )

def cmd_otc(update, ctx):
    # Optional arg to set payout threshold (e.g. /otc 85)
    try:
        if ctx.args:
            mp = int(ctx.args[0])
            STATE["min_payout"] = max(50, min(100, mp))
    except Exception:
        pass
    update.message.reply_text(
        f"⚙️ OTC strict mode ON\n"
        f"- Confidence ≥ {int(STATE['threshold']*100)}%\n"
        f"- Confluence: 4/4 (Trend, RSI50, MACD, EMA20/50)\n"
        f"- ATR floor: {STATE['atr_floor']}\n"
        f"- Min payout: {STATE['min_payout']}%\n"
        f"- Cooldown: {STATE['cooldown_min']}m | Gap: {STATE['min_signal_gap_min']}m"
    )

def cmd_autopoll(update, ctx):
    if STATE["autopoll_running"]:
        update.message.reply_text("ℹ️ Already running.")
        return
    chat_id = update.effective_chat.id
    STATE["autopoll_running"] = True
    STATE["last_signal_time"] = None
    STATE["pair_last_signal_time"] = {}
    t = threading.Thread(target=autopoll_loop, args=(ctx.bot, chat_id), daemon=True)
    STATE["autopoll_thread"] = t
    t.start()
    update.message.reply_text("▶️ Autopoll started. Waiting for candle close…")

def cmd_stop(update, ctx):
    STATE["autopoll_running"] = False
    update.message.reply_text("⏹️ Autopoll stopped.")

# ========= POCKET OPTION PAYOUT LISTENER (guest) =========
def start_payout_listener():
    try:
        import websocket
    except Exception:
        print("[payout] websocket-client not installed; payouts disabled")
        return

    def _ws_thread():
        while True:
            ws = None
            try:
                ws = websocket.create_connection(PO_WS_URL, timeout=10)
                print("[payout] connected to", PO_WS_URL)

                # Some WS servers require a subscription message.
                # We'll try a few generic messages; harmless if ignored.
                try:
                    ws.send(json.dumps({"event": "ping"}))
                    ws.send(json.dumps({"subscribe": "assets"}))
                    ws.send(json.dumps({"action": "subscribe", "channel": "assets"}))
                except Exception:
                    pass

                while True:
                    msg = ws.recv()
                    if not msg:
                        break

                    # Try to parse payouts from various shapes:
                    try:
                        data = json.loads(msg)
                    except Exception:
                        # Some socket.io payloads embed JSON after a numeric prefix
                        # e.g., '42["assets", {...}]'
                        if msg.startswith("42"):
                            try:
                                bracket = msg.find("[")
                                if bracket >= 0:
                                    data = json.loads(msg[bracket:])
                                else:
                                    continue
                            except Exception:
                                continue
                        else:
                            continue

                    # Handle list of assets
                    if isinstance(data, list):
                        # e.g., ["assets", [{...}, {...}]]
                        payloads = []
                        for item in data:
                            if isinstance(item, dict):
                                payloads.append(item)
                            elif isinstance(item, list):
                                payloads.extend([x for x in item if isinstance(x, dict)])
                        if payloads:
                            _ingest_payout_payloads(payloads)

                    elif isinstance(data, dict):
                        # direct dict or nested under 'data'/'payload'
                        if "data" in data and isinstance(data["data"], list):
                            _ingest_payout_payloads(data["data"])
                        elif "payload" in data and isinstance(data["payload"], list):
                            _ingest_payout_payloads(data["payload"])
                        else:
                            _ingest_payout_payloads([data])

            except Exception as e:
                print("[payout] WS error:", e)
                time.sleep(10)
            finally:
                try:
                    if ws:
                        ws.close()
                except Exception:
                    pass

    def _ingest_payout_payloads(items):
        for item in items:
            if not isinstance(item, dict):
                continue
            sym = item.get("asset") or item.get("symbol") or item.get("pair") or item.get("name")
            pct = item.get("profit") or item.get("payout") or item.get("rate") or item.get("percent")
            if sym and pct is not None:
                try:
                    pct = int(float(pct))
                except Exception:
                    continue
                sym_norm = str(sym).upper().replace("/", "")
                # normalize 'EURUSDOTC' -> 'EURUSD-OTC'
                if "OTC" in sym_norm and not sym_norm.endswith("-OTC"):
                    sym_norm = sym_norm.replace("OTC", "-OTC")
                if sym_norm in STATE["watchlist"]:
                    STATE["payouts"][sym_norm] = pct

    threading.Thread(target=_ws_thread, daemon=True).start()

# ========= BOOT (webhook for Render) =========
def main():
    # Start payout listener (guest). If it fails, bot still works with manual /otc threshold.
    start_payout_listener()

    updater = Updater(TELEGRAM_BOT_TOKEN, use_context=True)
    dp = updater.dispatcher
    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("otc",   cmd_otc))
    dp.add_handler(CommandHandler("autopoll", cmd_autopoll))
    dp.add_handler(CommandHandler("stop",  cmd_stop))
    dp.add_handler(CallbackQueryHandler(on_button))

    updater.start_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=TELEGRAM_BOT_TOKEN,
        webhook_url=f"{PUBLIC_URL}/{TELEGRAM_BOT_TOKEN}"
    )
    updater.idle()

if __name__ == "__main__":
    main()