import os
import asyncio
import time
from datetime import datetime, timezone
from typing import Dict, Tuple, Optional, List

import aiohttp
import numpy as np
import pandas as pd
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, ContextTypes, CallbackQueryHandler
)

# ============== CONFIG ==============
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()

# Data providers (primary -> Twelve, fallback -> AlphaVantage)
TWELVE_API_KEY = os.environ.get("TWELVE_API_KEY", "").strip()
ALPHAVANTAGE_API_KEY = os.environ.get("ALPHAVANTAGE_API_KEY", "").strip()

DEFAULT_INTERVAL_SEC = 120       # autosignal cadence (seconds)
CACHE_TTL_SEC = 30               # candle cache to reduce API burn
ALLOWED_INTERVALS = {"1min", "5min", "15min"}

state = {
    "autosignal_task": {},   # chat_id -> asyncio.Task
    "user_prefs": {},        # user_id -> {"bet_amount": float, "bet_duration": int}
    "cache": {}              # (provider, symbol, interval) -> {"ts": float, "df": DataFrame}
}

# ============== UTILS ==============
def normalize_symbol(sym: str) -> str:
    """EURUSD-OTC -> EUR/USD; GBPJPY -> GBP/JPY; already EUR/USD stays."""
    s = sym.upper().replace(" ", "")
    if s.endswith("-OTC"):
        s = s[:-4]
    if "/" not in s and 6 <= len(s) <= 7:
        s = f"{s[:3]}/{s[3:]}"
    return s

def av_fx_symbol(sym: str) -> Optional[tuple]:
    """AlphaVantage needs FROM/TO like EUR/USD -> (EUR, USD); return None if not FX-like."""
    s = normalize_symbol(sym)
    if "/" in s and len(s.split("/")[0]) == 3 and len(s.split("/")[1]) in (3, 4):
        base, quote = s.split("/")
        return base, quote
    return None

def now_utc_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def format_pct(x: float) -> str:
    return f"{x*100:.2f}%"

# ============== DATA FETCH ==============
async def fetch_twelve_series(session: aiohttp.ClientSession, symbol: str, interval: str = "1min", outputsize: int = 60) -> Optional[pd.DataFrame]:
    key = ("twelve", symbol, interval)
    c = state["cache"].get(key)
    t = time.time()
    if c and t - c["ts"] < CACHE_TTL_SEC:
        return c["df"]

    url = "https://api.twelvedata.com/time_series"
    params = {"symbol": symbol, "interval": interval, "outputsize": str(outputsize),
              "apikey": TWELVE_API_KEY, "format": "JSON", "order": "ASC"}
    try:
        async with session.get(url, params=params, timeout=20) as resp:
            data = await resp.json()
    except Exception as e:
        print("Twelve HTTP error:", e); return None

    if not isinstance(data, dict) or "values" not in data:
        print("Twelve bad response:", data); return None

    try:
        df = pd.DataFrame(data["values"])
        for col in ["open", "high", "low", "close"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["datetime"] = pd.to_datetime(df["datetime"])
        df = df.sort_values("datetime").reset_index(drop=True)
    except Exception as e:
        print("Twelve parse error:", e); return None

    state["cache"][key] = {"ts": t, "df": df}
    return df

async def fetch_alpha_series(session: aiohttp.ClientSession, raw_symbol: str, interval: str = "1min", outputsize: int = 60) -> Optional[pd.DataFrame]:
    """AlphaVantage FX_INTRADAY fallback. Only for FX pairs."""
    pair = av_fx_symbol(raw_symbol)
    if not pair:
        return None
    base, quote = pair
    key = ("alpha", f"{base}/{quote}", interval)
    c = state["cache"].get(key)
    t = time.time()
    if c and t - c["ts"] < CACHE_TTL_SEC:
        return c["df"]

    # AV supported intervals: 1min, 5min, 15min, 30min, 60min
    av_interval = interval if interval in {"1min", "5min", "15min", "30min", "60min"} else "1min"
    url = "https://www.alphavantage.co/query"
    params = {
        "function": "FX_INTRADAY",
        "from_symbol": base,
        "to_symbol": quote,
        "interval": av_interval,
        "apikey": ALPHAVANTAGE_API_KEY,
        "outputsize": "compact"
    }
    try:
        async with session.get(url, params=params, timeout=20) as resp:
            data = await resp.json()
    except Exception as e:
        print("Alpha HTTP error:", e); return None

    # Data is under "Time Series FX (1min)" etc.
    series_key = next((k for k in data.keys() if k.startswith("Time Series")), None)
    if not series_key or not isinstance(data.get(series_key), dict):
        print("Alpha bad response:", data); return None

    try:
        rows = []
        for ts, ohlc in data[series_key].items():
            rows.append({
                "datetime": pd.to_datetime(ts),
                "open": float(ohlc.get("1. open", "nan")),
                "high": float(ohlc.get("2. high", "nan")),
                "low": float(ohlc.get("3. low", "nan")),
                "close": float(ohlc.get("4. close", "nan")),
            })
        df = pd.DataFrame(rows).sort_values("datetime").reset_index(drop=True)
    except Exception as e:
        print("Alpha parse error:", e); return None

    state["cache"][key] = {"ts": t, "df": df}
    return df

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    x = df.copy()
    close = x["close"].astype(float).values

    def ema(series, span): return pd.Series(series).ewm(span=span, adjust=False).mean().values
    x["ema5"] = ema(close, 5)
    x["ema14"] = ema(close, 14)

    delta = np.diff(close, prepend=close[0])
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    roll_up = pd.Series(gain).rolling(14).mean()
    roll_down = pd.Series(loss).rolling(14).mean()
    rs = roll_up / (roll_down.replace(0, np.nan))
    rsi = 100.0 - (100.0 / (1.0 + rs))
    x["rsi14"] = rsi.fillna(50.0)

    x["mom5"] = x["close"].pct_change(5).fillna(0)
    return x

def decide_signal(last_row: pd.Series):
    ema5 = float(last_row["ema5"]); ema14 = float(last_row["ema14"])
    rsi = float(last_row["rsi14"]);  mom5 = float(last_row["mom5"])
    score = 0.0
    score += 0.4 if ema5 > ema14 else (-0.4 if ema5 < ema14 else 0.0)
    score += 0.3 if rsi > 60 else (-0.3 if rsi < 40 else 0.0)
    score += 0.2 if mom5 > 0.001 else (-0.2 if mom5 < -0.001 else 0.0)
    score = max(-1.0, min(1.0, score))
    action = "BUY" if score >= 0.15 else ("SELL" if score <= -0.15 else "NEUTRAL")
    return action, abs(score), {"rsi14": rsi, "mom5": mom5, "ema_diff": ema5 - ema14}

def pretty_signal_text(symbol: str, interval: str, action: str, conf: float, last_price: float, extras: Dict[str, float], is_otc: bool, now_str: str) -> str:
    otc_note = " (OTC focus)" if is_otc else ""
    return "\n".join([
        f"📈 *Signal* for *{symbol}*{otc_note}",
        f"Interval: `{interval}`   •   Time: {now_str}",
        f"Price: *{last_price:.5f}*",
        f"Action: *{action}*   •   Confidence: *{conf:.2f}*",
        f"RSI(14): {extras['rsi14']:.1f}   •   Momentum(5): {format_pct(extras['mom5'])}   •   EMA diff: {extras['ema_diff']:.5f}"
    ])

async def ensure_tokens(update: Update) -> bool:
    if not TELEGRAM_BOT_TOKEN:
        await update.effective_message.reply_text("❌ TELEGRAM_BOT_TOKEN missing in env")
        return False
    if not (TWELVE_API_KEY or ALPHAVANTAGE_API_KEY):
        await update.effective_message.reply_text("❌ Add TWELVE_API_KEY or ALPHAVANTAGE_API_KEY in env")
        return False
    return True

# ============== COMMANDS ==============
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Hey! I'm your FX signal bot.\n\n"
        "• /signal <symbol> [interval] (e.g., /signal EURUSD-OTC 1min)\n"
        "• /autosignal <symbol> [seconds] (default 120)\n"
        "• /stop — stop autosignal\n"
        "• /status — autosignal status\n"
        "• /setbet <amount> <duration_sec>\n"
        "• /sources — which data providers are set\n"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_cmd(update, context)

async def sources_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    td = "✅ TwelveData" if TWELVE_API_KEY else "❌ TwelveData"
    av = "✅ AlphaVantage" if ALPHAVANTAGE_API_KEY else "❌ AlphaVantage"
    await update.effective_message.reply_text(f"Configured:\n- {td}\n- {av}\n(OTC pairs map to spot FX symbols.)")

async def setbet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) != 2:
        await update.effective_message.reply_text("Format: /setbet <amount> <duration_sec>")
        return
    try:
        amount = float(args[0]); duration = int(args[1])
    except:
        await update.effective_message.reply_text("Provide numeric values, e.g. /setbet 5 6")
        return
    uid = update.effective_user.id
    state["user_prefs"][uid] = {"bet_amount": amount, "bet_duration": duration}
    await update.effective_message.reply_text(f"Saved bet prefs: ${amount:.2f} for {duration}s")

async def get_signal_text(raw_symbol: str, interval: str) -> Optional[str]:
    is_otc = raw_symbol.upper().endswith("-OTC")
    symbol_std = normalize_symbol(raw_symbol)

    async with aiohttp.ClientSession() as session:
        df = None
        if TWELVE_API_KEY:
            df = await fetch_twelve_series(session, symbol_std, interval=interval, outputsize=60)
        if (df is None or df.empty) and ALPHAVANTAGE_API_KEY:
            df = await fetch_alpha_series(session, raw_symbol, interval=interval, outputsize=60)

    if df is None or df.empty:
        return None

    x = compute_indicators(df)
    last = x.iloc[-1]
    action, conf, extras = decide_signal(last)
    return pretty_signal_text(
        symbol=raw_symbol, interval=interval, action=action, conf=conf,
        last_price=float(last["close"]), extras=extras, is_otc=is_otc, now_str=now_utc_str()
    )

async def signal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_tokens(update): return
    if not context.args:
        await update.effective_message.reply_text("Usage: /signal <symbol> [interval]\nExample: /signal EURUSD-OTC 1min")
        return
    raw_symbol = context.args[0]
    interval = context.args[1] if len(context.args) > 1 else "1min"
    if interval not in ALLOWED_INTERVALS:
        await update.effective_message.reply_text(f"Interval must be one of: {', '.join(sorted(ALLOWED_INTERVALS))}")
        return

    text = await get_signal_text(raw_symbol, interval)
    if not text:
        await update.effective_message.reply_text("Couldn't fetch data (rate limit or bad symbol). Try again shortly.")
        return

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👆 BUY", callback_data="act:BUY"),
         InlineKeyboardButton("👇 SELL", callback_data="act:SELL")],
        [InlineKeyboardButton("↻ Refresh", callback_data=f"refresh:{raw_symbol}:{interval}")]
    ])
    await update.effective_message.reply_markdown(text, reply_markup=kb, disable_web_page_preview=True)

async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    data = q.data or ""
    if data.startswith("act:"):
        await q.edit_message_reply_markup(reply_markup=None)
        await q.message.reply_text(f"Noted: *{data[4:]}* (no real trade placed).", parse_mode="Markdown")
    elif data.startswith("refresh:"):
        _, raw_symbol, interval = data.split(":")
        class FakeContext: pass
        fc = FakeContext(); fc.args = [raw_symbol, interval]
        fake = Update(q.update_id, message=q.message)
        await signal_cmd(fake, fc)  # reuse handler

async def autosignal_loop(chat_id: int, raw_symbol: str, every_sec: int, app: Application):
    interval = "1min"
    while True:
        try:
            text = await get_signal_text(raw_symbol, interval)
            if text:
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("👆 BUY", callback_data="act:BUY"),
                                             InlineKeyboardButton("👇 SELL", callback_data="act:SELL")]])
                await app.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown", reply_markup=kb)
            else:
                await app.bot.send_message(chat_id=chat_id, text=f"Data fetch failed for {raw_symbol}. Retrying…")
        except asyncio.CancelledError:
            break
        except Exception as e:
            try: await app.bot.send_message(chat_id=chat_id, text=f"Error: {e}")
            except: pass
        await asyncio.sleep(max(30, int(every_sec)))  # safe lower bound

async def autosignal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_tokens(update): return
    if not context.args:
        await update.effective_message.reply_text("Usage: /autosignal <symbol> [seconds]\nExample: /autosignal EURUSD-OTC 180")
        return
    raw_symbol = context.args[0]
    every = int(context.args[1]) if len(context.args) > 1 else DEFAULT_INTERVAL_SEC
    if every < 30:
        await update.effective_message.reply_text("Interval too short. Use >= 30 seconds.")
        return
    chat_id = update.effective_chat.id
    prev = state["autosignal_task"].get(chat_id)
    if prev and not prev.done():
        prev.cancel()
        await update.effective_message.reply_text("Stopped previous autosignal. Starting new…")
    app: Application = context.application
    task = asyncio.create_task(autosignal_loop(chat_id, raw_symbol, every, app))
    state["autosignal_task"][chat_id] = task
    await update.effective_message.reply_text(f"Autosignal started for {raw_symbol} every {every}s. Use /stop to end.")

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    task = state["autosignal_task"].get(chat_id)
    if task and not task.done():
        task.cancel(); await update.effective_message.reply_text("Autosignal stopped.")
    else:
        await update.effective_message.reply_text("No autosignal task is running.")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    task = state["autosignal_task"].get(chat_id)
    if task and not task.done():
        await update.effective_message.reply_text("Autosignal is *running* ✅", parse_mode="Markdown")
    else:
        await update.effective_message.reply_text("Autosignal is *not running* ❌", parse_mode="Markdown")

# legacy alias (you used this name before)
async def stoppool_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await stop_cmd(update, context)

def build_app() -> Application:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN env var is required")
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("sources", sources_cmd))
    app.add_handler(CommandHandler("signal", signal_cmd))
    app.add_handler(CommandHandler("autosignal", autosignal_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("setbet", setbet_cmd))
    app.add_handler(CommandHandler("stoppool", stoppool_cmd))
    app.add_handler(CallbackQueryHandler(handle_button))
    return app

def main():
    app = build_app()
    print("Bot is starting…")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()