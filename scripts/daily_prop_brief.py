#!/usr/bin/env python3
"""
Daily Prop Trading Automation - Kabun Method
Author: Azman Taufik (adapted from Kabun Seminar Notes)
Repository: dewatagiri/position-sizing

Generates automated daily briefings for Telegram:
- Mode 'night': Prompt 2 (Chart Structure Review night before)
- Mode 'presession': Prompt 3 (Event/Macro Check) & Prompt 7 (Daily Plan + Gated Checklist)
- Mode 'orb': Opening range (first 15 min) stats for US equities after the cash open
"""

import os
import sys
import html
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import datetime
from zoneinfo import ZoneInfo

# Secrets come from the environment only; strip stray whitespace/newlines from pasted values.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TELEGRAM_CHUNK = 3800  # Telegram caps a message at 4096 chars

NY_TZ = ZoneInfo("America/New_York")
MYT = datetime.timezone(datetime.timedelta(hours=8))

# Crypto candles: Kraken public OHLC (Binance is geo-blocked on US GitHub runners).
KRAKEN_OHLC_URL = "https://api.kraken.com/0/public/OHLC"
KRAKEN_PAIRS_URL = "https://api.kraken.com/0/public/AssetPairs"
KRAKEN_PAIRS = {"BTCUSDT": "XBTUSDT", "ETHUSDT": "ETHUSDT"}
KRAKEN_ALIASES = {"BTC": "XBT", "DOGE": "XDG"}
KRAKEN_INTERVALS = {"1d": 1440, "4h": 240, "1h": 60}
KRAKEN_MIN_INTERVAL = 0.4  # seconds between calls; Kraken rate-limits public endpoints

# Equities / metals candles: Yahoo Finance chart API (no key needed).
YAHOO_HOSTS = ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]
YAHOO_URL = "https://{host}{path}"
YAHOO_MIN_INTERVAL = 0.3

# Assets that get the full range / taps / deviation breakdown.
STRUCTURE_ASSETS = [
    {"title": "BTC/USDT", "src": "kraken", "sym": "BTCUSDT"},
    {"title": "ETH/USDT", "src": "kraken", "sym": "ETHUSDT"},
    {"title": "GOLD (GC futures)", "src": "yahoo", "sym": "GC=F"},
    {"title": "SILVER (SI futures)", "src": "yahoo", "sym": "SI=F"},
]

# Compact one-liners: first 5 listed on Kraken per group; the rest are reserves.
ALTS_PER_GROUP = 5
ALT_GROUPS = [
    ("L1 / L2", ["SOL", "XRP", "DOGE", "AVAX", "SUI", "NEAR", "ADA", "DOT"]),
    ("AI / Compute", ["TAO", "FET", "ICP", "FIL", "GRT", "AR", "VIRTUAL"]),
    ("DeFi / Infra", ["LINK", "UNI", "HYPE", "INJ", "ONDO", "JUP", "PENDLE", "ENA"]),
    ("Memes", ["PEPE", "SHIB", "BONK", "WIF", "TRUMP", "FLOKI", "PUMP", "BOME"]),
]

# Opening range breakout
ORB_MINUTES = 15
ORB_CONTEXT = ["SPY", "QQQ"]
ORB_TICKERS = ["NVDA", "TSLA", "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NFLX", "MSTR", "HOOD"]
ORB_WINDOW = (9 * 60 + 46, 10 * 60 + 40)  # scheduled runs only fire inside this ET window


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

_last_call = {}

def _throttle(key, gap):
    wait = gap - (time.monotonic() - _last_call.get(key, -1e9))
    if wait > 0:
        time.sleep(wait)
    _last_call[key] = time.monotonic()

def _kraken_request(url):
    """GET a Kraken public endpoint and return its `result`; retries on rate limiting."""
    for attempt in range(3):
        _throttle("kraken", KRAKEN_MIN_INTERVAL)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = json.loads(resp.read().decode())
        except Exception as e:
            raise BriefingError(f"Kraken request failed: {e}") from e
        errors = body.get("error")
        if errors and any("Too many requests" in x for x in errors) and attempt < 2:
            time.sleep(2 * (attempt + 1))
            continue
        if errors:
            raise BriefingError(f"Kraken error: {errors}")
        return body.get("result", {})

def fetch_klines(symbol="BTCUSDT", interval="1d", limit=30):
    """Fetch candles from Kraken's public OHLC API (works from US IPs)."""
    pair = KRAKEN_PAIRS.get(symbol, symbol)
    minutes = KRAKEN_INTERVALS.get(interval)
    if minutes is None:
        raise BriefingError(f"Unsupported interval: {interval}")
    try:
        result = _kraken_request(f"{KRAKEN_OHLC_URL}?pair={pair}&interval={minutes}")
    except BriefingError as e:
        raise BriefingError(f"{symbol} {interval}: {e}") from None
    # Format: result[<pair>] = [[time, open, high, low, close, vwap, volume, count], ...]
    rows = next((v for k, v in result.items() if k != "last"), [])
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
        raise BriefingError(f"{symbol} {interval}: no candles returned")
    return candles

def kraken_altnames():
    """Set of pair names Kraken lists (e.g. 'SOLUSDT')."""
    result = _kraken_request(KRAKEN_PAIRS_URL)
    return {v.get("altname") for v in result.values()}

def resolve_kraken_pair(base, altnames):
    alias = KRAKEN_ALIASES.get(base, base)
    for quote in ("USDT", "USD"):
        if f"{alias}{quote}" in altnames:
            return f"{alias}{quote}"
    return None

def _yahoo_json(path):
    err = "unknown"
    for host in YAHOO_HOSTS:
        for _ in range(2):
            _throttle("yahoo", YAHOO_MIN_INTERVAL)
            req = urllib.request.Request(YAHOO_URL.format(host=host, path=path),
                                         headers={"User-Agent": "Mozilla/5.0"})
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    return json.loads(resp.read().decode())
            except urllib.error.HTTPError as e:
                err = f"HTTP {e.code}"
                if e.code != 429:
                    break
                time.sleep(3)
            except Exception as e:
                err = type(e).__name__
                break
    raise BriefingError(f"Yahoo request failed ({err})")

def fetch_yahoo(symbol, interval, range_):
    """Fetch candles from Yahoo Finance's chart API (regular session only)."""
    path = (f"/v8/finance/chart/{urllib.parse.quote(symbol, safe='=^')}"
            f"?interval={interval}&range={range_}&includePrePost=false")
    try:
        chart = _yahoo_json(path).get("chart") or {}
    except BriefingError as e:
        raise BriefingError(f"{symbol} {interval}: {e}") from None
    if chart.get("error"):
        raise BriefingError(f"{symbol} {interval}: Yahoo error {chart['error']}")
    result = (chart.get("result") or [None])[0]
    if not result:
        raise BriefingError(f"{symbol} {interval}: no data returned")
    quote = result["indicators"]["quote"][0]
    candles = []
    for i, t in enumerate(result.get("timestamp") or []):
        o, h, l, c = (quote[k][i] for k in ("open", "high", "low", "close"))
        if None in (o, h, l, c):
            continue
        candles.append({"open": o, "high": h, "low": l, "close": c,
                        "volume": quote["volume"][i] or 0.0, "time": t})
    if not candles:
        raise BriefingError(f"{symbol} {interval}: no candles returned")
    return candles

def resample_4h(candles):
    """Aggregate 1h candles into 4h buckets (epoch-aligned)."""
    buckets = {}
    for c in candles:
        buckets.setdefault(c["time"] // 14400, []).append(c)
    return [{
        "open": b[0]["open"],
        "high": max(x["high"] for x in b),
        "low": min(x["low"] for x in b),
        "close": b[-1]["close"],
        "volume": sum(x["volume"] for x in b),
        "time": b[0]["time"],
    } for _, b in sorted(buckets.items())]

def fmt_price(p):
    if p >= 1:
        return f"{p:,.2f}"
    if p >= 0.01:
        return f"{p:.4f}"
    return f"{p:.8f}"

def load_candles(asset):
    if asset["src"] == "kraken":
        sym = asset["sym"]
        return fetch_klines(sym, "1d", 30), fetch_klines(sym, "4h", 42), fetch_klines(sym, "1h", 24)
    h1 = fetch_yahoo(asset["sym"], "60m", "1mo")
    return fetch_yahoo(asset["sym"], "1d", "3mo")[-30:], resample_4h(h1)[-42:], h1[-24:]

def analyze_structure(asset):
    """Identify Highs, Lows, Range boundaries, and key deviation checks."""
    daily, h4, h1 = load_candles(asset)

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
            deviation_state = f"⚠️ Bull Trap / Deviation above High ({fmt_price(candle['high'])}) -> closed back inside!"
            break
        elif candle["low"] < range_low and candle["close"] > range_low:
            deviation_state = f"⚠️ Bear Trap / Deviation below Low ({fmt_price(candle['low'])}) -> closed back inside!"
            break

    return {
        "title": asset["title"],
        "label": asset["title"].split("/")[0].split(" ")[0],
        "current_price": current_price,
        "range_high": range_high,
        "range_low": range_low,
        "range_mid": range_mid,
        "high_taps": high_taps,
        "low_taps": low_taps,
        "deviation_state": deviation_state
    }

def analyze_alt(base, pair):
    """One request per alt: 24h change and position inside the 5-day (120h) range."""
    c = fetch_klines(pair, "1h", 121)
    if len(c) < 25:
        raise BriefingError(f"{base}: only {len(c)} hourly candles")
    price = c[-1]["close"]
    window = c[-120:]
    hi = max(x["high"] for x in window)
    lo = min(x["low"] for x in window)
    return {
        "base": base,
        "price": price,
        "chg24": (price / c[-25]["close"] - 1) * 100,
        "pos": (price - lo) / (hi - lo) * 100 if hi > lo else 50.0,
    }

def collect(items, fn, label):
    """Run fn over items; a failing item is recorded, not fatal."""
    results, failures = [], []
    for it in items:
        try:
            results.append(fn(it))
        except BriefingError as e:
            failures.append(str(e))
        except (KeyError, ValueError, IndexError, TypeError, ZeroDivisionError) as e:
            failures.append(f"{label(it)}: unexpected data ({type(e).__name__})")
    return results, failures

def collect_structures():
    return collect(STRUCTURE_ASSETS, analyze_structure, lambda a: a["sym"])

def collect_alts():
    """Returns (groups, notes, failures); groups = [(title, [alt dicts])]."""
    try:
        altnames = kraken_altnames()
    except BriefingError as e:
        return [], [], [f"alt list: {e}"]
    groups, notes, failures = [], [], []
    for title, bases in ALT_GROUPS:
        picks, skipped = [], []
        for b in bases:
            if len(picks) == ALTS_PER_GROUP:
                break
            pair = resolve_kraken_pair(b, altnames)
            if pair:
                picks.append((b, pair))
            else:
                skipped.append(b)
        if skipped:
            notes.append(f"{title}: not on Kraken, skipped {', '.join(skipped)}")
        rows, fails = collect(picks, lambda bp: analyze_alt(*bp), lambda bp: bp[0])
        groups.append((title, rows))
        failures += fails
    return groups, notes, failures

def alts_text(groups, notes):
    if not groups:
        return ""
    parts = ["🪙 <b>ALTS</b> (24h change · position in 5-day range)"]
    for title, rows in groups:
        lines = [f"<b>{title}</b>"]
        for r in rows:
            flag = " 🔼" if r["pos"] >= 90 else " 🔽" if r["pos"] <= 10 else ""
            lines.append(f"• <b>{r['base']}</b> ${fmt_price(r['price'])} | {r['chg24']:+.1f}% | {r['pos']:.0f}%{flag}")
        parts.append("\n".join(lines))
    if notes:
        parts.append("\n".join(f"<i>{html.escape(n)}</i>" for n in notes))
    return "\n\n".join(parts)

def warn_block(failures):
    if not failures:
        return ""
    lines = "\n".join(f"• {html.escape(f)}" for f in failures)
    return f"⚠️ <b>Data failed:</b>\n{lines}"

def split_message(text, limit=TELEGRAM_CHUNK):
    """Split on blank lines (then single lines) so HTML tags are never cut."""
    chunks, cur = [], ""
    pieces = []
    for para in text.split("\n\n"):
        if len(para) <= limit:
            pieces.append(para)
        else:
            pieces += para.split("\n")
    for p in pieces:
        sep = "\n\n" if cur else ""
        if cur and len(cur) + len(sep) + len(p) > limit:
            chunks.append(cur)
            cur = p
        else:
            cur += sep + p
    if cur:
        chunks.append(cur)
    return chunks

def _send_one(text):
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

def send_telegram_message(text):
    """Send text formatted with HTML to Telegram, split to fit the size cap. Raises BriefingError on failure."""
    missing = [n for n, v in (("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN), ("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID)) if not v]
    if missing:
        raise BriefingError(f"Missing or empty environment variable(s): {', '.join(missing)}")
    for chunk in split_message(text):
        _send_one(chunk)

def myt_date_str():
    now_myt = datetime.datetime.now(MYT)
    return now_myt.strftime("%d %b %Y (%a)")

def generate_night_review():
    """Prompt 2: Chart Structure Review (night before). Returns (text, failures)."""
    structs, failures = collect_structures()
    groups, notes, alt_failures = collect_alts()
    failures += alt_failures

    msg = f"🌙 <b>KABUN NIGHT-BEFORE STRUCTURE REVIEW</b>\n"
    msg += f"📅 <b>Date:</b> {myt_date_str()} | 21:00 MYT\n"
    msg += f"━━━━━━━━━━━━━━━━━━━━━\n\n"

    for coin in structs:
        msg += f"📊 <b>{coin['title']} Structure</b>\n"
        msg += f"• <b>Live Price:</b> ${fmt_price(coin['current_price'])}\n"
        msg += f"• <b>Range High:</b> ${fmt_price(coin['range_high'])} (~{coin['high_taps']} taps)\n"
        msg += f"• <b>Range Low:</b> ${fmt_price(coin['range_low'])} (~{coin['low_taps']} taps)\n"
        msg += f"• <b>Mid-Range:</b> ${fmt_price(coin['range_mid'])}\n"
        msg += f"• <b>Deviation Status:</b> {coin['deviation_state']}\n\n"

    alts = alts_text(groups, notes)
    if alts:
        msg += alts + "\n\n"

    msg += f"🎯 <b>Kabun Setup Criteria Reminder:</b>\n"
    msg += f"1. Minimum 2 taps each side confirmed on 4H/Daily.\n"
    msg += f"2. Enter on <b>3rd tap ONLY</b> after a clear deviation on Lower Timeframe (15m/1h).\n"
    msg += f"3. Minimum 3:1 R:R (never take 2.5:1).\n\n"
    msg += f"🔗 <b>Prepare Size & Plan:</b> <a href='https://dewatagiri.github.io/position-sizing/prop.html'>Prop Firm Planner</a>\n"
    warn = warn_block(failures)
    if warn:
        msg += "\n" + warn
    return msg, failures

def generate_presession_brief():
    """Prompt 3 & Prompt 7: Event Check & Daily Plan with Checklist. Returns (text, failures)."""
    structs, failures = collect_structures()
    groups, notes, alt_failures = collect_alts()
    failures += alt_failures
    prices = " | ".join(f"{c['label']}: ${fmt_price(c['current_price'])}" for c in structs) or "n/a"

    msg = f"☀️ <b>KABUN PRE-SESSION BRIEF & DAILY PLAN</b>\n"
    msg += f"📅 <b>Date:</b> {myt_date_str()} | 08:30 MYT\n"
    msg += f"━━━━━━━━━━━━━━━━━━━━━\n\n"

    msg += f"🚨 <b>1. Pre-Session Event & Volatility Check</b>\n"
    msg += f"• {prices}\n"
    msg += f"• Verify no scheduled CPI / FOMC / NFP releases during your entry window.\n"
    msg += f"• Rule: If major high-impact news spikes against daily loss limit, sit out!\n\n"

    alts = alts_text(groups, notes)
    if alts:
        msg += alts + "\n\n"

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
    warn = warn_block(failures)
    if warn:
        msg += "\n" + warn
    return msg, failures

def analyze_orb(symbol):
    """Opening range (first ORB_MINUTES of the NY cash session) for the latest session in the data."""
    candles = fetch_yahoo(symbol, "5m", "5d")
    days = {}
    for c in candles:
        dt = datetime.datetime.fromtimestamp(c["time"], NY_TZ)
        days.setdefault(dt.date(), []).append((dt, c))
    dates = sorted(days)
    open_min = 9 * 60 + 30

    def opening_bars(day):
        return [c for dt, c in sorted(days[day], key=lambda x: x[0])
                if open_min <= dt.hour * 60 + dt.minute < open_min + ORB_MINUTES]

    need = ORB_MINUTES // 5
    session = dates[-1]
    orb = opening_bars(session)
    if len(orb) < need:
        raise BriefingError(f"{symbol}: opening range incomplete ({len(orb)}/{need} bars)")
    or_high = max(c["high"] for c in orb)
    or_low = min(c["low"] for c in orb)
    or_vol = sum(c["volume"] for c in orb)

    gap = None
    if len(dates) > 1:
        prev_close = sorted(days[dates[-2]], key=lambda x: x[0])[-1][1]["close"]
        gap = (orb[0]["open"] / prev_close - 1) * 100
    prior = [sum(c["volume"] for c in opening_bars(d)) for d in dates[:-1][-4:] if len(opening_bars(d)) >= need]
    avg_prior = sum(prior) / len(prior) if prior else 0
    return {
        "symbol": symbol,
        "session": session,
        "price": sorted(days[session], key=lambda x: x[0])[-1][1]["close"],
        "or_high": or_high,
        "or_low": or_low,
        "or_range_pct": (or_high / or_low - 1) * 100,
        "gap": gap,
        "rvol": or_vol / avg_prior if avg_prior > 0 else None,
    }

def orb_skip_reason(now_ny):
    """Why a scheduled ORB run should do nothing (the cron fires for both EDT and EST)."""
    if now_ny.weekday() >= 5:
        return "weekend"
    minute = now_ny.hour * 60 + now_ny.minute
    if not (ORB_WINDOW[0] <= minute < ORB_WINDOW[1]):
        return f"outside ORB window ({now_ny:%H:%M} ET)"
    return None

def orb_line(r):
    price, hi, lo = r["price"], r["or_high"], r["or_low"]
    if price > hi:
        status = f"🟢 above OR high ({(price / hi - 1) * 100:+.2f}%)"
    elif price < lo:
        status = f"🔴 below OR low ({(price / lo - 1) * 100:+.2f}%)"
    else:
        status = "⚪ inside range"
    gap = f"{r['gap']:+.1f}%" if r["gap"] is not None else "n/a"
    rvol = f"{r['rvol']:.1f}x" if r["rvol"] is not None else "n/a"
    return (f"<b>{r['symbol']}</b> ${fmt_price(price)} | OR {fmt_price(lo)}–{fmt_price(hi)} ({r['or_range_pct']:.2f}%)\n"
            f"   {status} | gap {gap} | OR vol {rvol}")

def generate_orb_brief(gated=False, today=None):
    """Opening range stats for US equities. Returns (text, failures).

    gated: scheduled run, so a missing session for today (market holiday) is an error.
    """
    today = today or datetime.datetime.now(NY_TZ).date()
    symbols = ORB_CONTEXT + ORB_TICKERS
    results, failures = collect(symbols, analyze_orb, lambda s: s)
    if gated:
        stale = [r for r in results if r["session"] != today]
        failures += [f"{r['symbol']}: no session for {today:%d %b} (market holiday?)" for r in stale]
        results = [r for r in results if r["session"] == today]
    if not results:
        return "🔔 <b>OPENING RANGE BREAKOUT</b>\n\n" + warn_block(failures), failures

    session = max(r["session"] for r in results)
    start = datetime.datetime.combine(session, datetime.time(9, 30), NY_TZ)
    end = start + datetime.timedelta(minutes=ORB_MINUTES)
    msg = f"🔔 <b>OPENING RANGE BREAKOUT</b> | {session:%a %d %b %Y}\n"
    msg += f"OR = first {ORB_MINUTES} min of the US session: 09:30–{end:%H:%M} ET "
    msg += f"({start.astimezone(MYT):%H:%M}–{end.astimezone(MYT):%H:%M} MYT)\n"
    msg += f"━━━━━━━━━━━━━━━━━━━━━\n\n"
    by_sym = {r["symbol"]: r for r in results}
    market = [by_sym[s] for s in ORB_CONTEXT if s in by_sym]
    stocks = [by_sym[s] for s in ORB_TICKERS if s in by_sym]
    if market:
        msg += "📈 <b>Market</b>\n" + "\n".join(orb_line(r) for r in market) + "\n\n"
    if stocks:
        msg += "🏢 <b>Equities</b>\n" + "\n".join(orb_line(r) for r in stocks) + "\n\n"
    msg += "<i>Gap = open vs prior close. OR vol = opening volume vs the last 4 sessions' average. Stats only, not a signal.</i>\n"
    warn = warn_block(failures)
    if warn:
        msg += "\n" + warn
    return msg, failures

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
    failures = []
    if mode == "night":
        text, failures = generate_night_review()
        messages = [text]
    elif mode == "presession":
        text, failures = generate_presession_brief()
        messages = [text, generate_sector_watchlist()]
    elif mode == "sectors":
        messages = [generate_sector_watchlist()]
    elif mode == "orb":
        gated = os.getenv("ORB_GATE") == "1"
        if gated:
            reason = orb_skip_reason(datetime.datetime.now(NY_TZ))
            if reason:
                print(f"Skipping ORB brief: {reason}")
                return
        text, failures = generate_orb_brief(gated)
        messages = [text]
    elif mode == "test":
        messages = ["🤖 <b>Kabun Daily Automation Test</b>\nBot connection active and ready!"]
    else:
        raise BriefingError(f"Unknown mode: {mode}")
    for text in messages:
        send_telegram_message(text)
    if failures:
        # The brief went out with what we had; fail the run so the gap is visible.
        raise BriefingError(f"{len(failures)} data fetch(es) failed: " + "; ".join(failures))

if __name__ == "__main__":
    try:
        main()
    except BriefingError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
