# otc-sentinel-bot — autosignals, strict-only alerts, 60s advance entry
# python-telegram-bot v21+

import asyncio
import time
from datetime import datetime, timezone
from typing import Optional, Tuple, Dict

import aiohttp
import numpy as np
import pandas as pd
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ================== HARD-CODED SECRETS (per your request) ==================
TELEGRAM_BOT_TOKEN   = "8471181182:AAEKGH1UASA5XvkXScb3jb5d1Yz19B8oJNM"
TWELVE_API_KEY       = "9aa4ea677d00474aa0c3223d0c812425"
ALPHAVANTAGE_API_KEY = "BM22MZEI0LL68RI6"
# ===========================================================================

# ---- Behavior ----
DEFAULT_AUTOSIGNAL_SEC = 120          # autosignal cadence
CACHE_TTL_SEC = 30                    # API cache to reduce burn
ALLOWED_INTERVALS = {"1min", "5min", "15min"}
STRICT_DEFAULT_ENABLED = True
STRICT_DEFAULT_THRESHOLD = 0.60       # only alert when confidence >= this

# ---- In-memory state ----
state = {
    "autosignal_task": {},            # chat_id -> asyncio.Task
    "cache": {},                      # (provider, symbol, interval) -> {"ts": float, "df": DataFrame}
    "strict": {},                     # chat_id -> {"enabled": bool, "threshold": float}
    "last_bar_alerted": {}            # (chat_id, symbol) -> last candle timestamp alerted
}

# ================== Utils ==================
def normalize_symbol(sym: str) -> str:
    s = sym.upper().replace(" ", "")
    if s.endswith("-OTC"):
        s = s[:-4]
    if "/" not in s and 6 <= len(s) <= 7:
        s = f"{s[:3]}/{s[3:]}"
    return s

def av_fx_symbol(sym: str) -> Optional[Tuple[str, str]]:
    s = normalize_symbol(sym)
    if "/" in s:
        a, b = s.split("/")
        if len(a) == 3 and len(b) in (3, 4):
            return a, b
    return None

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def now_utc_str() -> str:
    return now_utc().strftime("%Y-%m-%d %H:%M:%S UTC")

def pct(x: float) -> str:
    return f"{x*100:.2f}%"

def conf_to_probability(conf: float) -> float:
    # Display mapping (0..1 -> 50..95%)
    return 0.50 + 0.45 * max(0.0, min(1.0, conf))

# ================== Data fetching (Twelve primary, Alpha fallback) ==================
async def fetch_twelve_series(session: aiohttp.ClientSession, symbol: str, interval="1min", outputsize=120):
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
        print("Twelve error:", e); return None

    if not isinstance(data, dict) or "values" not in data:
        print("Twelve bad resp:", data); return None

    try:
        df = pd.DataFrame(data["values"])
        for ccol in ["open", "high", "low", "close"]:
            df[ccol] = pd.to_numeric(df[ccol], errors="coerce")
        df["datetime"] = pd.to_datetime(df["datetime"])
        df = df.sort_values("datetime").reset_index(drop=True)
    except Exception as e:
        print("Twelve parse:", e); return None

    state["cache"][key] = {"ts": t, "df": df}
    return df

async def fetch_alpha_series(session: aiohttp.ClientSession, raw_symbol: str, interval="1min"):
    pair = av_fx_symbol(raw_symbol)
    if not pair:
        return None
    base, quote = pair
    av_int = interval if interval in {"1min", "5min", "15min", "30min", "60min"} else "1min"

    key = ("alpha", f"{base}/{quote}", av_int)
    c = state["cache"].get(key)
    t = time.time()
    if c and t - c["ts"] < CACHE_TTL_SEC:
        return c["df"]

    url = "https://www.alphavantage.co/query"
    params = {"function": "FX_INTRADAY",
              "from_symbol": base, "to_symbol": quote,
              "interval": av_int, "apikey": ALPHAVANTAGE_API_KEY,
              "outputsize": "compact"}
    try:
        async with session.get(url, params=params, timeout=20) as resp:
            data = await resp.json()
    except Exception as e:
        print("Alpha error:", e); return None

    series_key = next((k for k in data.keys() if k.startswith("Time Series")), None)
    if not series_key or not isinstance(data.get(series_key), dict):
        print("Alpha bad resp:", data); return None

    try:
        rows = []
        for ts, ohlc in data[series_key].items():
            rows.append({
                "datetime": pd.to_datetime(ts),
                "open": float(ohlc.get("1. open", "nan")),
                "high": float(ohlc.get("2. high", "nan")),
                "low":  float(ohlc.get("3. low", "nan")),
                "close":float(ohlc.get("4. close", "nan")),
            })
        df = pd.DataFrame(rows).sort_values("datetime").reset_index(drop=True)
    except Exception as e:
        print("Alpha parse:", e); return None

    state["cache"][key] = {"ts": t, "df": df}
    return df

async def get_series(raw_symbol: str, interval="1min"):
    """Try Twelve first, then Alpha."""
    symbol_std = normalize_symbol(raw_symbol)
    async with aiohttp.ClientSession() as session:
        df = None
        if TWELVE_API_KEY:
            df = await fetch_twelve_series(session, symbol_std, interval=interval, outputsize=120)
        if (df is None or df.empty) and ALPHAVANTAGE_API_KEY:
            df = await fetch_alpha_series(session, raw_symbol, interval=interval)
    return df

# ================== Indicators & signal ==================
def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    x = df.copy()
    close = x["close"].astype(float).values

    def ema(series, span):
        return pd.Series(series).ewm(span=span, adjust=False).mean().values

    x["ema5"] = ema(close, 5)
    x["ema14"] = ema(close, 14)

    delta = np.diff(close, prepend=close[0])
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    roll_up = pd.Series(gain).rolling(14).mean()
    roll_down = pd.Series(loss).rolling(14).mean()
    rs = roll_up / (roll_down.replace(0, np.nan))
    rsi = 100.0 - (100.0 / (1.0 + rs))
    x["rsi14"] = pd.Series(rsi).fillna(50.0)

    x["mom5"] = x["close"].pct_change(5).fillna(0.0)

    return x

def decide_signal(last_row: pd.Series):
    ema5 = float(last_row["ema5"])
    ema14 = float(last_row["ema14"])
    rsi = float(last_row["rsi14"])
    mom5 = float(last_row["mom5"])

    score = 0.0
    score += 0.4 if ema5 > ema14 else (-0.4 if ema5 < ema14 else 0.0)
    score += 0.3 if rsi > 60 else (-0.3 if rsi < 40 else 0.0)
    score += 0.2 if mom5 > 0.001 else (-0.2 if mom5 < -0.001 else 0.0)
    score = max(-1.0, min(1.0, score))

    action = "BUY" if score >= 0.15 else ("SELL" if score <= -0.15 else "NEUTRAL")
    conf = abs(score)
    prob = conf_to_probability(conf)  # for display
    return action, conf, prob, {"rsi14": rsi, "mom5": mom5, "ema_diff": (ema5 - ema14)}

def build_advance_text(raw_symbol: str, interval: str, last_price: float, action: str, conf: float, prob: float, extras: Dict[str, float], candle_time: str):
    return (
        f"📊 *Advance Signal (60s)* for *{raw_symbol}*\n"
        f"Interval: `{interval}` • Candle: {candle_time} UTC\n"
        f"Price: *{last_price:.5f}*\n"
        f"Action: *{action}* • Confidence: *{conf:.2f}* • Prob: *{pct(prob)}*\n"
        f"RSI(14): {extras['rsi14']:.1f} • Mom(5): {pct(extras['mom5'])} • EMA diff: {extras['ema_diff']:.5f}\n"
        f"⌛ Entry in ~60s…"
    )

def build_entry_text(raw_symbol: str, action: str):
    return f"🚦 *ENTER NOW* → *{action}* on *{raw_symbol}*"

# ================== Bot Commands ==================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    # init strict prefs if missing
    if chat_id not in state["strict"]:
        state["strict"][chat_id] = {"enabled": STRICT_DEFAULT_ENABLED, "threshold": STRICT_DEFAULT_THRESHOLD}
    st = state["strict"][chat_id]
    await update.effective_message.reply_markdown(
        "OTC Sentinel Bot ready.\n\n"
        "Commands:\n"
        "• `/autosignal <symbol> [seconds]`  e.g. `/autosignal EURUSD-OTC 180`\n"
        "• `/stop`  — stop autosignals\n"
        "• `/status` — show status & strict settings\n"
        "• `/strict on|off [threshold]` — e.g. `/strict on 0.65`\n"
        "\nNotes:\n"
        "- OTC pairs map to spot FX (EURUSD-OTC → EUR/USD).\n"
        "- Only sends alerts when strict threshold is met.\n"
        f"- Current strict: *{'ON' if st['enabled'] else 'OFF'}*, threshold *{st['threshold']:.2f}*"
    )

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    running = chat_id in state["autosignal_task"] and not state["autosignal_task"][chat_id].done()
    st = state["strict"].get(chat_id, {"enabled": STRICT_DEFAULT_ENABLED, "threshold": STRICT_DEFAULT_THRESHOLD})
    await update.effective_message.reply_markdown(
        f"Autosignal: *{'RUNNING ✅' if running else 'STOPPED ❌'}*\n"
        f"Strict mode: *{'ON' if st['enabled'] else 'OFF'}*  •  Threshold: *{st['threshold']:.2f}*"
    )

async def strict_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in state["strict"]:
        state["strict"][chat_id] = {"enabled": STRICT_DEFAULT_ENABLED, "threshold": STRICT_DEFAULT_THRESHOLD}
    args = [a.lower() for a in context.args]
    st = state["strict"][chat_id]

    if not args:
        await update.effective_message.reply_text(f"Strict is {'ON' if st['enabled'] else 'OFF'} @ {st['threshold']:.2f}")
        return

    if args[0] in ("on", "off"):
        st["enabled"] = (args[0] == "on")
        if len(args) > 1:
            try:
                th = float(args[1]); st["threshold"] = max(0.0, min(1.0, th))
            except: pass
        await update.effective_message.reply_text(f"Strict set to {args[0].upper()} @ {st['threshold']:.2f}")
    else:
        try:
            th = float(args[0]); st["threshold"] = max(0.0, min(1.0, th))
            await update.effective_message.reply_text(f"Strict threshold set to {st['threshold']:.2f}")
        except:
            await update.effective_message.reply_text("Usage: /strict on|off [threshold]   or   /strict 0.65")

async def autosignal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in state["strict"]:
        state["strict"][chat_id] = {"enabled": STRICT_DEFAULT_ENABLED, "threshold": STRICT_DEFAULT_THRESHOLD}

    if not context.args:
        await update.effective_message.reply_text("Usage: /autosignal <symbol> [seconds]\nExample: /autosignal EURUSD-OTC 180")
        return
    raw_symbol = context.args[0]
    every = int(context.args[1]) if len(context.args) > 1 else DEFAULT_AUTOSIGNAL_SEC
    if every < 30: every = 30

    # cancel old
    t = state["autosignal_task"].get(chat_id)
    if t and not t.done():
        t.cancel()
    task = asyncio.create_task(autosignal_loop(chat_id, raw_symbol, every, context.application))
    state["autosignal_task"][chat_id] = task
    await update.effective_message.reply_text(f"Autosignal started for {raw_symbol} every {every}s. I’ll only message when there’s a valid signal.")

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    t = state["autosignal_task"].get(chat_id)
    if t and not t.done():
        t.cancel()
        await update.effective_message.reply_text("Autosignal stopped.")
    else:
        await update.effective_message.reply_text("No autosignal is running.")

# ================== Core loop ==================
async def autosignal_loop(chat_id: int, raw_symbol: str, every_sec: int, app: Application):
    interval = "1min"  # trade timing built around 1-minute bars
    symbol_std = normalize_symbol(raw_symbol)

    # reset last alerted bar for this chat/symbol
    state["last_bar_alerted"][(chat_id, raw_symbol)] = None

    while True:
        try:
            df = await get_series(raw_symbol, interval=interval)
            if df is None or df.empty:
                await app.bot.send_message(chat_id, f"Data fetch failed for {raw_symbol}. Retrying…")
            else:
                x = compute_indicators(df)
                last = x.iloc[-1]
                candle_ts = pd.to_datetime(last["datetime"]).to_pydatetime().replace(tzinfo=None)
                last_alerted = state["last_bar_alerted"].get((chat_id, raw_symbol))

                # only consider a new signal on a new bar
                if (last_alerted is None) or (candle_ts != last_alerted):
                    action, conf, prob, extras = decide_signal(last)
                    strict = state["strict"].get(chat_id, {"enabled": STRICT_DEFAULT_ENABLED, "threshold": STRICT_DEFAULT_THRESHOLD})
                    threshold = strict["threshold"] if strict["enabled"] else 0.0

                    if action != "NEUTRAL" and conf >= threshold:
                        # record that we alerted this bar
                        state["last_bar_alerted"][(chat_id, raw_symbol)] = candle_ts

                        # Send 60s advance alert
                        advance = (
                            f"📣 *Signal Found* ({raw_symbol})\n"
                            f"Time: {now_utc_str()}\n"
                            f"{'OTC focus — ' if raw_symbol.upper().endswith('-OTC') else ''}"
                            f"Action: *{action}* • Confidence: *{conf:.2f}* • Prob: *{pct(prob)}*\n"
                            f"RSI(14): {extras['rsi14']:.1f} • Mom(5): {pct(extras['mom5'])} • EMA diff: {extras['ema_diff']:.5f}\n"
                            f"Price: *{float(last['close']):.5f}* • Interval: `{interval}`\n"
                            f"⌛ *ENTER IN 60s* (I’ll remind you)"
                        )
                        await app.bot.send_message(chat_id, advance, parse_mode="Markdown")

                        # Schedule entry reminder in 60s (detached task)
                        async def entry_ping():
                            try:
                                await asyncio.sleep(60)
                                await app.bot.send_message(chat_id, f"🚦 *ENTER NOW* → *{action}* on *{raw_symbol}*", parse_mode="Markdown")
                            except asyncio.CancelledError:
                                pass
                        asyncio.create_task(entry_ping())
                    # else: no message (strict-only)
            await asyncio.sleep(max(30, every_sec))
        except asyncio.CancelledError:
            break
        except Exception as e:
            try:
                await app.bot.send_message(chat_id, f"Error: {e}")
            except:
                pass
            await asyncio.sleep(max(30, every_sec))

# ================== App wiring ==================
def build_app() -> Application:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("strict", strict_cmd))
    app.add_handler(CommandHandler("autosignal", autosignal_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    return app

def main():
    app = build_app()
    print("OTC Sentinel Bot starting…")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()