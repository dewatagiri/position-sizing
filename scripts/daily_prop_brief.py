#!/usr/bin/env python3
"""
Daily Prop Trading Automation - Kabun Method
Author: Azman Taufik (adapted from Kabun Seminar Notes)
Repository: dewatagiri/position-sizing

Generates automated daily briefings for Telegram:
- Mode 'night': Prompt 2 (Chart Structure Review night before)
- Mode 'presession': Prompt 3 (Event/Macro Check) & Prompt 7 (Daily Plan + Gated Checklist)
"""

import os
import sys
import json
import urllib.error
import urllib.request
import datetime

# Secrets come from the environment only; strip stray whitespace/newlines from pasted values.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

KRAKEN_OHLC_URL = "https://api.kraken.com/0/public/OHLC"
# Kraken pair names and OHLC interval minutes (Binance is geo-blocked on US GitHub runners).
KRAKEN_PAIRS = {"BTCUSDT": "XBTUSDT", "ETHUSDT": "ETHUSDT"}
KRAKEN_INTERVALS = {"1d": 1440, "4h": 240, "1h": 60}


class BriefingError(Exception):
    """Raised when market data cannot be fetched or Telegram cannot be reached."""

# (title, [sector ETFs], [example leaders])
SECTORS = [
    ("01 Semiconductors / Memory", ["SMH", "DRAM", "SOXX", "XSD"], ["NVDA", "AMD", "AVGO", "MU", "SNDK", "INTC"]),
    ("02 AI Infrastructure / Data Centers", ["DTCR", "IGV", "PSI", "PSTG", "CIBR"], ["DELL", "SMCI", "VRT", "ANET"]),
    ("03 Software / Cybersecurity", ["IGV", "CIBR", "HACK", "SKYY"], ["PLTR", "CRWD", "PANW", "FTNT", "NET"]),
    ("04 Power / Grid / Nuclear", ["GRID", "XLI", "NLR", "PAVE", "URA"], ["GEV", "CEG", "ETN", "VRT", "OKLO"]),
    ("05 Crypto / Fintech", ["ARKF", "WGMI", "BLOK", "BITO", "IBIT"], ["HOOD", "COIN", "CRCL", "MSTR"]),
    ("06 Space / Defense", ["ARKX", "ITA", "UFO", "XAR"], ["ASTS", "RKLB", "LMT", "RTX", "BA"]),
    ("07 Quantum", ["QTUM"], ["IONQ", "RGTI", "QBTS", "QUBT"]),
    ("08 Robotics / Automation", ["BOTZ", "ARKQ"], ["TSLA", "PLTR", "ACHR", "TER"]),
    ("09 Real Assets / Resources", ["GLD", "SLV", "COPX", "REMX"], ["FCX", "TECK", "SCCO", "MP", "ALB"]),
    ("10 Economy / Risk", ["IWM", "XLF", "XLE", "JETS", "SPY", "QQQ"], ["IWM", "XLF", "XLE", "JETS"]),
    ("11 Health / Biotech", ["XBI", "XLV", "IBB", "ARKG"], ["VRTX", "LLY", "MRNA", "ABBV"]),
]

WEEKEND_CHECKLIST = [
    "What is making new highs?",
    "What is holding the 8/21 trend?",
    "What is outperforming $SPY / $QQQ?",
    "Where is relative strength showing up?",
    "What is breaking out of a base?",
    "Where is volume expanding?",
]

def fetch_klines(symbol="BTCUSDT", interval="1d", limit=30):
    """Fetch candles from Kraken's public OHLC API (works from US IPs)."""
    pair = KRAKEN_PAIRS.get(symbol)
    minutes = KRAKEN_INTERVALS.get(interval)
    if pair is None or minutes is None:
        raise BriefingError(f"Unsupported symbol/interval: {symbol} {interval}")
    url = f"{KRAKEN_OHLC_URL}?pair={pair}&interval={minutes}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode())
    except Exception as e:
        raise BriefingError(f"Error fetching {symbol} {interval}: {e}") from e
    if body.get("error"):
        raise BriefingError(f"Kraken error for {symbol} {interval}: {body['error']}")
    # Format: result[<pair>] = [[time, open, high, low, close, vwap, volume, count], ...]
    rows = next((v for k, v in body.get("result", {}).items() if k != "last"), [])
    candles = [
        {
            "open": float(d[1]),
            "high": float(d[2]),
            "low": float(d[3]),
            "close": float(d[4]),
            "volume": float(d[6]),
            "time": d[0],
        }
        for d in rows[-limit:]
    ]
    if not candles:
        raise BriefingError(f"No candles returned for {symbol} {interval}")
    return candles

def analyze_structure(symbol="BTCUSDT"):
    """Identify Highs, Lows, Range boundaries, and key deviation checks."""
    daily = fetch_klines(symbol, "1d", 30)
    h4 = fetch_klines(symbol, "4h", 42) # ~7 days
    h1 = fetch_klines(symbol, "1h", 24)

    current_price = h1[-1]["close"]
    
    # 4H Range Estimation (last 30 bars)
    recent_h4 = h4[-30:]
    range_high = max(c["high"] for c in recent_h4)
    range_low = min(c["low"] for c in recent_h4)
    range_mid = (range_high + range_low) / 2.0

    # Count approximate touches/taps (within 0.75% of high/low)
    high_threshold = range_high * 0.9925
    low_threshold = range_low * 1.0075
    
    high_taps = sum(1 for c in recent_h4 if c["high"] >= high_threshold)
    low_taps = sum(1 for c in recent_h4 if c["low"] <= low_threshold)

    # Deviation detection (1H wick outside range closing back inside)
    last_1h = h1[-3:]
    deviation_state = "No active deviation"
    for candle in reversed(last_1h):
        if candle["high"] > range_high and candle["close"] < range_high:
            deviation_state = f"⚠️ Bull Trap / Deviation above High ({candle['high']:.2f}) -> closed back inside!"
            break
        elif candle["low"] < range_low and candle["close"] > range_low:
            deviation_state = f"⚠️ Bear Trap / Deviation below Low ({candle['low']:.2f}) -> closed back inside!"
            break

    return {
        "symbol": symbol.replace("USDT", ""),
        "current_price": current_price,
        "range_high": range_high,
        "range_low": range_low,
        "range_mid": range_mid,
        "high_taps": high_taps,
        "low_taps": low_taps,
        "deviation_state": deviation_state
    }

def send_telegram_message(text):
    """Send text formatted with HTML/Markdown to Telegram. Raises BriefingError on failure."""
    missing = [n for n, v in (("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN), ("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID)) if not v]
    if missing:
        raise BriefingError(f"Missing or empty environment variable(s): {', '.join(missing)}")
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10):
            print("Telegram sent successfully.")
    except urllib.error.HTTPError as e:
        # Report status and Telegram's description only; never the URL (it contains the token).
        try:
            desc = json.loads(e.read().decode()).get("description", "")
        except Exception:
            desc = ""
        raise BriefingError(f"Telegram HTTP {e.code}: {desc}") from None
    except Exception as e:
        raise BriefingError(f"Failed to send Telegram message: {type(e).__name__}") from None

def generate_night_review():
    """Prompt 2: Chart Structure Review (night before)"""
    btc = analyze_structure("BTCUSDT")
    eth = analyze_structure("ETHUSDT")
    now_myt = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    date_str = now_myt.strftime("%d %b %Y (%a)")

    msg = f"🌙 <b>KABUN NIGHT-BEFORE STRUCTURE REVIEW</b>\n"
    msg += f"📅 <b>Date:</b> {date_str} | 21:00 MYT\n"
    msg += f"━━━━━━━━━━━━━━━━━━━━━\n\n"

    for coin in [btc, eth]:
        msg += f"📊 <b>{coin['symbol']}/USDT Structure</b>\n"
        msg += f"• <b>Live Price:</b> ${coin['current_price']:,.2f}\n"
        msg += f"• <b>Range High:</b> ${coin['range_high']:,.2f} (~{coin['high_taps']} taps)\n"
        msg += f"• <b>Range Low:</b> ${coin['range_low']:,.2f} (~{coin['low_taps']} taps)\n"
        msg += f"• <b>Mid-Range:</b> ${coin['range_mid']:,.2f}\n"
        msg += f"• <b>Deviation Status:</b> {coin['deviation_state']}\n\n"

    msg += f"🎯 <b>Kabun Setup Criteria Reminder:</b>\n"
    msg += f"1. Minimum 2 taps each side confirmed on 4H/Daily.\n"
    msg += f"2. Enter on <b>3rd tap ONLY</b> after a clear deviation on Lower Timeframe (15m/1h).\n"
    msg += f"3. Minimum 3:1 R:R (never take 2.5:1).\n\n"
    msg += f"🔗 <b>Prepare Size & Plan:</b> <a href='https://dewatagiri.github.io/position-sizing/prop.html'>Prop Firm Planner</a>\n"
    return msg

def generate_presession_brief():
    """Prompt 3 & Prompt 7: Event Check & Daily Plan with Checklist"""
    btc = analyze_structure("BTCUSDT")
    eth = analyze_structure("ETHUSDT")
    now_myt = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    date_str = now_myt.strftime("%d %b %Y (%a)")

    msg = f"☀️ <b>KABUN PRE-SESSION BRIEF & DAILY PLAN</b>\n"
    msg += f"📅 <b>Date:</b> {date_str} | 08:30 MYT\n"
    msg += f"━━━━━━━━━━━━━━━━━━━━━\n\n"

    msg += f"🚨 <b>1. Pre-Session Event & Volatility Check</b>\n"
    msg += f"• BTC: ${btc['current_price']:,.2f} | ETH: ${eth['current_price']:,.2f}\n"
    msg += f"• Verify no scheduled CPI / FOMC / NFP releases during your entry window.\n"
    msg += f"• Rule: If major high-impact news spikes against daily loss limit, sit out!\n\n"

    msg += f"⏱ <b>2. Daily Schedule (MYT Timestamps)</b>\n"
    msg += f"• <b>09:00 - 11:30:</b> Asian Session review & range marking\n"
    msg += f"• <b>14:00 - 18:00:</b> London Open (Prime deviation & sweep window)\n"
    msg += f"• <b>20:30 - 23:00:</b> NY Session (Scale out / BE management)\n"
    msg += f"• <b>23:30 MYT:</b> Hard cutoff - log journal & stop trading\n\n"

    msg += f"📋 <b>3. Gated Pre-Trade Checklist (All must be YES):</b>\n"
    msg += f"[ ] HTF structure clear (Weekly > Daily > 4H > 1H)?\n"
    msg += f"[ ] Valid 2+2 taps with 3rd tap deviation confirmed?\n"
    msg += f"[ ] Minimum 3:1 R:R available (never 2.5:1)?\n"
    msg += f"[ ] Fixed 2% risk, max 5x leverage applied?\n"
    msg += f"[ ] Stop Loss placed (Price + 7-Day Time Stop)?\n"
    msg += f"[ ] Scale-in planned (50% entry / 50% retest)?\n"
    msg += f"[ ] Stop to breakeven after TP2 ready?\n"
    msg += f"[ ] Journal entry prepared?\n\n"

    msg += f"🛠 <b>Open Calculator:</b> <a href='https://dewatagiri.github.io/position-sizing/prop.html'>Bitfunded Prop Planner</a>\n"
    return msg

def generate_sector_watchlist():
    """Sector ETF + leader watchlist, with the weekend checklist on Sat/Sun MYT."""
    now_myt = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    msg = f"🗂 <b>SECTOR WATCHLIST</b> | {now_myt.strftime('%d %b %Y (%a)')}\n"
    msg += f"━━━━━━━━━━━━━━━━━━━━━\n\n"
    for title, etfs, leaders in SECTORS:
        msg += f"<b>{title}</b>\n"
        msg += f"• ETFs: {' '.join(etfs)}\n"
        msg += f"• Leaders: {' '.join(leaders)}\n\n"
    if now_myt.weekday() >= 5:
        msg += f"✅ <b>Weekend Checklist</b>\n"
        for i, q in enumerate(WEEKEND_CHECKLIST, 1):
            msg += f"{i}. {q}\n"
    return msg

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "night"
    if mode == "night":
        messages = [generate_night_review()]
    elif mode == "presession":
        messages = [generate_presession_brief(), generate_sector_watchlist()]
    elif mode == "sectors":
        messages = [generate_sector_watchlist()]
    elif mode == "test":
        messages = ["🤖 <b>Kabun Daily Automation Test</b>\nBot connection active and ready!"]
    else:
        raise BriefingError(f"Unknown mode: {mode}")
    for text in messages:
        send_telegram_message(text)

if __name__ == "__main__":
    try:
        main()
    except BriefingError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
