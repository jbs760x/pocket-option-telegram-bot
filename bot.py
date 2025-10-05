# bot.py — Pocket Option signal helper (manual only)
# - Commands: /start, /help, /pologin, /check, /signal, /multisignal, /watch
# - Data: Twelve Data primary -> Alpha Vantage fallback
# - OTC aliases like EURUSD-OTC handled (EUR/USD to data vendors)
# - NO AUTO-TRADING. It only tells you BUY/SELL + confidence.
# - python-telegram-bot v20 (ApplicationBuilder, no Updater)

import os
import re
import math
import asyncio
import logging
from datetime import datetime, timezone, timedelta

import aiohttp
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

# ─── YOUR STUFF (from our chat) ────────────────────────────────────────────────
BOT_TOKEN = "8471181182:AAFEhPc59AvzNsnuPbj-N2PatGbvgZnnd_0"
ADMIN_ID  = 7814662315  # your Telegram user id

# Market data keys
TWELVE_KEY = "9aa4ea677d00474aa0c3223d0c812425"
ALPHA_KEY  = "BM22MZEIOLL68RI6"
DATA_ORDER = ["twelve", "alpha"]  # priority

# Pocket Option (used only to remember who you are; no trading)
PO_UID_DEFAULT  = "93269888"
PO_SSID_DEFAULT = "d7a8a43d4618a7227c6ed769f8fd9975"

# in-memory state
STATE = {
    "po_uid": PO_UID_DEFAULT,
    "po_ssid": PO_SSID_DEFAULT,
    "watch": ["EURUSD-OTC","GBPUSD-OTC","USDJPY-OTC","AUDCAD-OTC"]
}

HELP_TEXT = (
    "🤖 Pocket Option Signals (manual only)\n\n"
    "Commands:\n"
    "/start – hello\n"
    "/help – show this\n"
    "/pologin SSID UID – save your Pocket Option session (optional)\n"
    "/check SYMBOL [tf=1min|5min|15min] – show indicators only\n"
    "/signal SYMBOL [tf=1min] – get BUY/SELL + confidence %\n"
    "/multisignal [tf=1min] – scan your watchlist and list any signals\n"
    "/watch add|remove|list|clear [SYMBOL] – manage watchlist\n\n"
    "Use OTC names like EURUSD-OTC. I won’t place trades—just tell you what to do."
)

# ─── Utilities ────────────────────────────────────────────────────────────────
def norm_symbol(sym: str) -> str:
    raw = sym.upper()
    if raw.endswith("-OTC"): raw = raw[:-4]
    raw = raw.replace("_", "")
    if "/" in raw: return raw
    if len(raw) == 6: return f"{raw[:3]}/{raw[3:]}"
    return raw

def disp_symbol(sym: str) -> str:
    return sym.upper()

def parse_cmd_amounts(txt: str):
    # not used for placing orders; kept for future
    m = re.match(r"^\s*([A-Za-z0-9/_\-.]+)\s+(\d+)\s+(\d+)\s*$", txt)
    if not m: return None
    s, amt, dur = m.groups()
    return s.upper(), float(amt), int(dur)

# ─── Indicators ───────────────────────────────────────────────────────────────
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
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(values)-1):
        ag = (ag*(period-1) + gains[i]) / period
        al = (al*(period-1) + losses[i]) / period
    if al == 0: return 100.0
    rs = ag/al
    return 100 - (100/(1+rs))

def decide_and_confidence(candles):
    """Return ('BUY'|'SELL'|None, confidence_float [0..1], diag)"""
    if len(candles) < 60: return None, 0.0, {"reason":"few candles"}
    closes = [float(c["close"]) for c in candles]
    opens  = [float(c["open"])  for c in candles]
    e50 = ema(closes, 50); r = rsi(closes, 14)
    if e50 is None or r is None: return None, 0.0, {"reason":"indicators n/a"}

    # momentum via last body vs median body of last 10 bars
    bodies = [abs(closes[i]-opens[i]) for i in range(-11, -1)]
    med = sorted(bodies)[5] if len(bodies) >= 10 else 0.0
    mom = (abs(closes[-1]-opens[-1])/(med+1e-9)) if med else 0.0
    mom_boost = min(mom, 2.0)/2.0  # 0..1

    trend_buy  = 1.0 if closes[-1] > e50 else 0.0
    trend_sell = 1.0 - trend_buy
    rsi_buy  = max(0.0, (30 - r)/30)      # oversold -> buy
    rsi_sell = max(0.0, (r - 70)/30)      # overbought -> sell

    buy_score  = 0.45*rsi_buy  + 0.40*trend_buy  + 0.15*mom_boost
    sell_score = 0.45*rsi_sell + 0.40*trend_sell + 0.15*mom_boost

    if buy_score < 0.35 and sell_score < 0.35:
        return None, 0.0, {"weak":True}

    if buy_score >= sell_score:
        prob = max(0.5, min(0.9, 0.5 + (buy_score - sell_score)))
        return "BUY", prob, {}
    else:
        prob = max(0.5, min(0.9, 0.5 + (sell_score - buy_score)))
        return "SELL", prob, {}

# ─── Data fetchers ────────────────────────────────────────────────────────────
async def fetch_twelvedata(symbol, interval="1min", limit=120):
    if not TWELVE_KEY: return [], "no TWELVE_KEY"
    td_symbol = norm_symbol(symbol)
    url = (
        "https://api.twelvedata.com/time_series"
        f"?symbol={td_symbol}&interval={interval}&outputsize={limit}&apikey={TWELVE_KEY}"
    )
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=20) as r:
                js = await r.json()
                if isinstance(js, dict) and js.get("status") == "error":
                    return [], js.get("message","TD error")
                vals = js.get("values")
                if not vals: return [], "TD: no values"
                # TD returns newest-first
                vals = list(reversed(vals))[-limit:]
                # ensure fields present
                out=[]
                for c in vals:
                    out.append({
                        "datetime": c.get("datetime"),
                        "open": float(c.get("open")),
                        "high": float(c.get("high")),
                        "low":  float(c.get("low")),
                        "close": float(c.get("close")),
                    })
                return out, None
    except Exception as e:
        return [], f"TD fail: {e}"

async def fetch_alphavantage(symbol, interval="1min", limit=120):
    if not ALPHA_KEY: return [], "no ALPHA_KEY"
    raw = norm_symbol(symbol).replace("/","")
    base, quote = raw[:3], raw[3:6]
    interval = interval if interval in {"1min","5min","15min","30min","60min"} else "1min"
    url = (
        "https://www.alphavantage.co/query"
        f"?function=FX_INTRADAY&from_symbol={base}&to_symbol={quote}"
        f"&interval={interval}&apikey={ALPHA_KEY}&outputsize=compact"
    )
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=20) as r:
                js = await r.json()
                key = next((k for k in js if "Time Series" in k), None)
                if not key: return [], js.get("Note") or js.get("Error Message") or "AV: no series"
                series = js[key]
                rows=[]
                for t,v in sorted(series.items()):
                    rows.append({
                        "datetime": t,
                        "open": float(v["1. open"]),
                        "high": float(v["2. high"]),
                        "low":  float(v["3. low"]),
                        "close": float(v["4. close"]),
                    })
                return rows[-limit:], None
    except Exception as e:
        return [], f"AV fail: {e}"

async def fetch_candles(symbol, interval="1min", limit=120):
    errors = []
    for prov in DATA_ORDER:
        if prov == "twelve":
            data, err = await fetch_twelvedata(symbol, interval, limit)
        else:
            data, err = await fetch_alphavantage(symbol, interval, limit)
        if data and not err: return data, None
        errors.append(f"{prov}: {err or 'no data'}")
    return [], " | ".join(errors)

# ─── Command handlers ─────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hi! I’m ready. Type /help for commands.")

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)

async def pologin_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # /pologin SSID UID
    if len(ctx.args) != 2:
        return await update.message.reply_text(
            "Usage: /pologin SSID UID\n\n"
            "Saved currently:\nSSID: "
            f"{STATE['po_ssid'][:6]}...  UID: {STATE['po_uid']}"
        )
    ssid, uid = ctx.args[0].strip(), ctx.args[1].strip()
    STATE["po_ssid"], STATE["po_uid"] = ssid, uid
    await update.message.reply_text("✅ Saved your Pocket Option session (manual mode only).")

async def check_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) not in (1,2):
        return await update.message.reply_text("Usage: /check SYMBOL [tf=1min|5min|15min]")
    sym = ctx.args[0].upper()
    tf  = (ctx.args[1] if len(ctx.args)==2 else "1min").lower()
    candles, err = await fetch_candles(sym, tf, 120)
    if err: return await update.message.reply_text(f"❌ {err}")
    closes = [c["close"] for c in candles]
    e50 = ema(closes, 50); r = rsi(closes, 14)
    await update.message.reply_text(
        f"📊 {disp_symbol(sym)} {tf}\n"
        f"Bars: {len(closes)}\n"
        f"EMA50: {round(e50,6) if e50 else 'n/a'}\n"
        f"RSI14: {round(r,2) if r else 'n/a'}"
    )

async def signal_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) not in (1,2):
        return await update.message.reply_text("Usage: /signal SYMBOL [tf=1min]")
    sym = ctx.args[0].upper()
    tf  = (ctx.args[1] if len(ctx.args)==2 else "1min").lower()
    candles, err = await fetch_candles(sym, tf, 120)
    if err: return await update.message.reply_text(f"❌ {err}")
    side, prob, _ = decide_and_confidence(candles)
    if not side:
        return await update.message.reply_text(f"🤷 No clear edge on {disp_symbol(sym)} ({tf}).")
    await update.message.reply_text(
        f"🎯 {disp_symbol(sym)} {tf}\n"
        f"Direction: **{side}**\n"
        f"Confidence: {int(prob*100)}%\n"
        f"Note: manual mode – place it yourself in Pocket Option."
    )

async def multisignal_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tf = (ctx.args[0] if ctx.args else "1min").lower()
    hits = []
    for sym in STATE["watch"]:
        candles, err = await fetch_candles(sym, tf, 120)
        if err or not candles: continue
        side, prob, _ = decide_and_confidence(candles)
        if side and prob >= 0.60:  # only list stronger ideas
            hits.append(f"{disp_symbol(sym)} → {side} ({int(prob*100)}%)")
        await asyncio.sleep(0.2)
    if hits:
        await update.message.reply_text("📋 Multi-scan:\n" + "\n".join(hits))
    else:
        await update.message.reply_text("No strong setups on the watchlist right now.")

async def watch_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sub = (ctx.args[0].lower() if ctx.args else "list")
    if sub == "add" and len(ctx.args) >= 2:
        s = ctx.args[1].upper()
        if s not in STATE["watch"]: STATE["watch"].append(s)
        return await update.message.reply_text("Added. Now: " + ", ".join(STATE["watch"]))
    if sub == "remove" and len(ctx.args) >= 2:
        s = ctx.args[1].upper()
        if s in STATE["watch"]: STATE["watch"].remove(s)
        return await update.message.reply_text("Removed. Now: " + (", ".join(STATE["watch"]) or "(empty)"))
    if sub == "clear":
        STATE["watch"].clear()
        return await update.message.reply_text("Cleared watchlist.")
    return await update.message.reply_text(", ".join(STATE["watch"]) or "(empty)")

# Fallback: allow quick free-text like "EURUSD-OTC"
async def echo_parse(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    if re.fullmatch(r"[A-Za-z0-9/_\-.]+", txt):
        # Treat as /signal SYMBOL
        update.message.text = f"/signal {txt}"
        return await signal_cmd(update, ctx)

# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("pologin", pologin_cmd))
    app.add_handler(CommandHandler("check", check_cmd))
    app.add_handler(CommandHandler("signal", signal_cmd))
    app.add_handler(CommandHandler("multisignal", multisignal_cmd))
    app.add_handler(CommandHandler("watch", watch_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo_parse))

    # polling (single instance). If you ever see "Conflict", make sure only one instance is running.
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()