"""
Trading Laboratory -> Buffet AI (technical chart analysis)
A subscriber sends a chart screenshot (WeBull, TradingView, ...) or writes "$TSLA 4h" in the Buffet AI
topic. Google Apps Script starts this script (GitHub Actions). It:
  1. downloads the picture and asks Gemini (vision) what is on it: ticker, timeframe, patterns, candles;
  2. downloads real prices for that ticker (Yahoo Finance) and CALCULATES RSI, divergences, ATR,
     support / resistance, candle patterns and a trade plan (entry, stop-loss, TP1-TP3);
  3. asks Gemini to explain everything in simple Uzbek, using only the calculated levels;
  4. replies with an annotated chart card and the text.
If the ticker cannot be read or has no data (crypto exchange pairs, futures, ...), the analysis is made
from the picture alone and the reply says so.
Not connected with Warren Buffett.
"""

import base64
import html
import io
import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

import numpy as np

# ---------------------------------------------------------------------------
# SETTINGS (you can edit these)
# ---------------------------------------------------------------------------

GEMINI_MODELS = ["gemini-flash-latest", "gemini-2.5-flash", "gemini-flash-lite-latest",
                 "gemini-2.5-flash-lite"]
GEMINI_BUDGET = 150           # seconds in total for each Gemini step
GEMINI_TIMEOUT = 90
RSI_LEN, ATR_LEN = 14, 14
PIVOT = 3                     # swing high/low = highest/lowest of 3 bars on each side
BARS_ON_CARD = 110
DISCLAIMER = ("⚠️ Investitsion maslahat emas. Darajalar texnik qoidalar asosida hisoblangan, savdo signali emas. "
              "Buffet AI — Trading Laboratory boti, Warren Buffett bilan bog'liq emas.")

# timeframe -> (yfinance interval, period, resample rule or None)
TIMEFRAMES = {
    "1m": ("1m", "5d", None), "3m": ("1m", "5d", "3min"), "5m": ("5m", "30d", None),
    "15m": ("15m", "60d", None), "30m": ("30m", "60d", None), "1h": ("60m", "180d", None),
    "2h": ("60m", "365d", "2h"), "4h": ("60m", "700d", "4h"), "1D": ("1d", "6y", None),
    "1W": ("1wk", "12y", None), "1M": ("1mo", "max", None),
}
TF_NAMES = {"1m": "1 daqiqa", "3m": "3 daqiqa", "5m": "5 daqiqa", "15m": "15 daqiqa", "30m": "30 daqiqa",
            "1h": "1 soat", "2h": "2 soat", "4h": "4 soat", "1D": "1 kun", "1W": "1 hafta", "1M": "1 oy"}
INDEX = {"SPX": "^GSPC", "SPX500": "^GSPC", "US500": "^GSPC", "NDX": "^NDX", "NAS100": "^NDX", "US100": "^NDX",
         "DJI": "^DJI", "US30": "^DJI", "VIX": "^VIX", "RUT": "^RUT", "IXIC": "^IXIC", "DXY": "DX-Y.NYB"}

# ---------------------------------------------------------------------------

BOT_TOKEN = re.sub(r"\s+", "", os.environ.get("BUFFET_BOT_TOKEN", ""))
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
FILE_ID = os.environ.get("FILE_ID", "").strip()
CAPTION = os.environ.get("CAPTION", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()
THREAD_ID = os.environ.get("THREAD_ID", "").strip()
REPLY_TO = os.environ.get("REPLY_TO", "").strip()
ACK_ID = os.environ.get("ACK_ID", "").strip()


# ----------------------------- Telegram -------------------------------------

def tg(method, payload=None, files=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    if files:
        boundary = uuid.uuid4().hex
        body = io.BytesIO()
        for k, v in (payload or {}).items():
            body.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n".encode())
            body.write((v if isinstance(v, str) else json.dumps(v)).encode("utf-8") + b"\r\n")
        for k, (name, data, ctype) in files.items():
            body.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{name}\"\r\n"
                       f"Content-Type: {ctype}\r\n\r\n".encode())
            body.write(data + b"\r\n")
        body.write(f"--{boundary}--\r\n".encode())
        data, ctype = body.getvalue(), f"multipart/form-data; boundary={boundary}"
    else:
        data, ctype = json.dumps(payload or {}).encode("utf-8"), "application/json"
    for _ in range(3):
        req = urllib.request.Request(url, data=data, headers={"Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            info = json.loads(e.read().decode("utf-8") or "{}")
            wait = info.get("parameters", {}).get("retry_after")
            if e.code == 429 and wait:
                time.sleep(wait + 1)
                continue
            raise RuntimeError(f"Telegram {method} {e.code}: {info.get('description')}")
    raise RuntimeError("Telegram: too many retries")


def where(reply=True):
    p = {"chat_id": CHAT_ID}
    if THREAD_ID:
        p["message_thread_id"] = int(THREAD_ID)
    if reply and REPLY_TO:
        p["reply_parameters"] = {"message_id": int(REPLY_TO), "allow_sending_without_reply": True}
    return p


def send_text(text, reply=True, html_mode=False):
    limit = 3900
    parts, rest = [], text
    while len(rest) > limit:
        cut = rest.rfind("\n\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        parts.append(rest[:cut])
        rest = rest[cut:].lstrip()
    parts.append(rest)
    for i, chunk in enumerate(parts):
        p = {**where(reply and i == 0), "text": chunk, "link_preview_options": {"is_disabled": True}}
        if html_mode:
            p["parse_mode"] = "HTML"
        tg("sendMessage", p)


def send_photo(png, caption):
    tg("sendPhoto", {**where(), "caption": caption[:1024], "parse_mode": "HTML"},
       files={"photo": ("buffet_ai.png", png, "image/png")})


def delete_ack():
    if ACK_ID:
        try:
            tg("deleteMessage", {"chat_id": CHAT_ID, "message_id": int(ACK_ID)})
        except Exception as exc:
            print(f"Could not delete the waiting message: {exc}")


def download_photo(file_id):
    info = tg("getFile", {"file_id": file_id})
    path = info["result"]["file_path"]
    with urllib.request.urlopen(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{path}", timeout=60) as resp:
        data = resp.read()
    mime = "image/png" if path.lower().endswith(".png") else "image/webp" if path.lower().endswith(".webp") \
        else "image/jpeg"
    return data, mime


# ----------------------------- Gemini ---------------------------------------

def gemini_json(parts, label):
    """Gemini answer forced to JSON. A busy or limited model is skipped at once."""
    body = json.dumps({"contents": [{"role": "user", "parts": parts}],
                       "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"}}).encode()
    deadline = time.time() + GEMINI_BUDGET
    last = "Gemini javob bermadi"
    dead = set()
    for round_no in range(2):
        for model in [m for m in GEMINI_MODELS if m not in dead]:
            left = deadline - time.time()
            if left < 10:
                raise RuntimeError(last + " (vaqt tugadi)")
            req = urllib.request.Request(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent", data=body,
                headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY})
            started = time.time()
            try:
                with urllib.request.urlopen(req, timeout=min(GEMINI_TIMEOUT, left)) as resp:
                    res = json.load(resp)
                text = "".join(p.get("text", "") for p in res["candidates"][0]["content"]["parts"])
                text = re.sub(r"^```(?:json)?|```$", "", text.strip()).strip()
                out = json.loads(text[text.find("{"):text.rfind("}") + 1])
                print(f"Gemini {label} {model}: {time.time() - started:.0f}s")
                return out
            except urllib.error.HTTPError as e:
                msg = e.read().decode("utf-8", "replace")[:300]
                print(f"Gemini {label} {model}: HTTP {e.code} {msg}")
                last = f"Gemini HTTP {e.code}"
                if e.code in (401, 403) or "API key" in msg:
                    raise RuntimeError("Gemini kaliti noto'g'ri (GEMINI_API_KEY)")
                if e.code == 404 or (e.code == 429 and "PerDay" in msg):
                    dead.add(model)
            except Exception as exc:
                print(f"Gemini {label} {model}: {exc}")
                last = str(exc)
        time.sleep(5)
    raise RuntimeError(last)


READ_PROMPT = """You see a screenshot of a trading chart (WeBull, TradingView or similar).
Read it carefully and answer ONLY with JSON:
{
 "is_chart": true/false,
 "symbol": "ticker exactly as shown, e.g. TSLA, BTCUSDT, EURUSD, ES1!, SPX; empty if not visible",
 "exchange": "NASDAQ / NYSE / BINANCE / ... or empty",
 "asset": "stock | etf | crypto | forex | futures | index | other",
 "timeframe": "one of 1m 3m 5m 15m 30m 1h 2h 4h 1D 1W 1M (best guess from the chart header / axis)",
 "last_price": number or null (the latest price shown),
 "price_high": number or null (highest price label visible on the right axis),
 "price_low": number or null (lowest price label visible on the right axis),
 "indicators_visible": ["RSI", "MACD", "EMA 20", ...],
 "rsi_value_visible": number or null
}
The user's caption (may contain the ticker or timeframe and then it wins): "%s"
"""

EXPLAIN_PROMPT = """You are "Buffet AI", the technical-analysis assistant of the Telegram group Trading Laboratory.
You are NOT Warren Buffett and never speak as him. Audience: beginner and intermediate traders in Uzbekistan.
Write in simple, clear UZBEK (Latin script). Trading terms (RSI, stop-loss, take profit, support, resistance,
divergence, double top, head and shoulders, doji, engulfing, ...) may stay in English, explained once if needed.

%s

Rules:
- Price levels, RSI values, ATR, divergences and the trade plan come ONLY from the CALCULATED DATA below
  (when it exists). Never invent other numbers. If the picture disagrees with the data, trust the data.
- From the picture you may describe chart patterns (triangle, flag, wedge, channel, double top/bottom,
  head and shoulders, cup and handle, ...), trend lines and drawings the user made.
- Say clearly when a pattern is only forming and not confirmed. Be honest when the picture is unclear.
- "extras" holds: "mc" = Monte Carlo replays of the plan (odds TP1 first / stop first / neither within
  horizon_bars, built from the instrument's own past bars), "history" = how the SAME Buffet AI rule did on
  past bars, "setups" = scanner setups, "risk" = warnings, "events" = earnings date and last move.
  Mention the odds honestly in plan_comment (a coin flip is a coin flip; never call it a sure thing), and
  say that replays are a simulation, not a prediction. If risk has a "block" item (earnings inside the
  plan), say clearly that the plan is risky because of it.
- "evidence" (daily charts) = what 5 years of real backtests on 957 US stocks say about THIS kind of signal.
  status "approved" = it matches a rule that worked in both test periods; "not_proven" = such signals did not
  make money over 5 years: say so clearly in summary and plan_comment and suggest a small position or waiting.
  The plan's stop/targets already use the exit that tested best. Mention the facts briefly and honestly.
- Short sentences. No markdown, no asterisks, no emoji.
- Each text field: 1-4 sentences.

Answer ONLY with JSON:
{
 "trend": "trend and market structure (higher highs/lows, EMA position)",
 "candles": "what the last candles say (use the calculated candle patterns, plus what you see)",
 "patterns": "chart patterns forming or completed, with the level that confirms them; 'aniq formatsiya yo'q' if none",
 "rsi": "RSI level and the divergence result in simple words",
 "volume": "volume comment, or empty if no data",
 "plan_comment": "why the entry, stop-loss and take profits are where they are; what invalidates the idea",
 "summary": "one-sentence conclusion"
}

CALCULATED DATA:
%s
"""


# ----------------------------- Market data ----------------------------------

def norm_tf(s):
    s = (s or "").strip()
    m = re.fullmatch(r"(\d+)\s*(m|min|h|hr|d|w|mo|M)?", s, re.I)
    if s.upper() in ("D", "1D", "DAY", "DAILY"):
        return "1D"
    if s.upper() in ("W", "1W", "WEEK", "WEEKLY"):
        return "1W"
    if s in ("M", "1M") and s != "1m":
        return "1M"
    if not m:
        return "1D"
    n, unit = int(m.group(1)), (m.group(2) or "m")
    if unit in ("M", "mo"):
        return "1M"
    unit = unit.lower()
    if unit in ("h", "hr"):
        return {1: "1h", 2: "2h", 3: "4h", 4: "4h"}.get(n, "4h" if n > 4 else "1h")
    if unit == "d":
        return "1D" if n < 5 else "1W"
    if unit == "w":
        return "1W"
    if n >= 240:
        return "4h"
    if n >= 120:
        return "2h"
    if n >= 60:
        return "1h"
    return min(("1m", "3m", "5m", "15m", "30m"), key=lambda k: abs(int(k[:-1]) - n))


def yahoo_symbol(symbol, asset):
    s = re.sub(r"^[A-Z]+:", "", (symbol or "").upper().strip()).replace(" ", "")
    if not s:
        return ""
    if s in INDEX:
        return INDEX[s]
    if asset == "futures" or s.endswith("1!") or s.endswith("!"):
        return re.sub(r"\d*!$", "", s) + "=F"
    fx = ("USD", "EUR", "JPY", "GBP", "CHF", "CAD", "AUD", "NZD")
    if asset == "forex" or (len(s) == 6 and s.isalpha() and s[:3] in fx and s[3:] in fx):
        return f"{s}=X"
    if asset == "crypto" or re.fullmatch(r"[A-Z]{2,6}(USDT|USDC|USD|BUSD)", s):
        base = re.sub(r"(USDT|USDC|BUSD|USD)$", "", s)
        return f"{base}-USD"
    return s.replace(".", "-")


def load_prices(ysym, tf):
    import yfinance as yf
    interval, period, rule = TIMEFRAMES[tf]
    df = yf.Ticker(ysym).history(period=period, interval=interval, auto_adjust=False)
    if df is None or df.empty:
        return None
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    if rule:
        df = df.resample(rule).agg({"Open": "first", "High": "max", "Low": "min", "Close": "last",
                                    "Volume": "sum"}).dropna()
    return df if len(df) >= 40 else None


def ema(x, n):
    out = np.empty_like(x)
    k = 2 / (n + 1)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = x[i] * k + out[i - 1] * (1 - k)
    return out


def rsi(close, n=RSI_LEN):
    d = np.diff(close, prepend=close[0])
    up, down = np.clip(d, 0, None), np.clip(-d, 0, None)
    au, ad = np.zeros_like(close), np.zeros_like(close)
    au[n], ad[n] = up[1:n + 1].mean(), down[1:n + 1].mean()
    for i in range(n + 1, len(close)):
        au[i] = (au[i - 1] * (n - 1) + up[i]) / n
        ad[i] = (ad[i - 1] * (n - 1) + down[i]) / n
    rs = np.divide(au, ad, out=np.full_like(au, np.inf), where=ad > 0)
    r = 100 - 100 / (1 + rs)
    r[:n] = np.nan
    return r


def atr(h, l, c, n=ATR_LEN):
    tr = np.maximum(h - l, np.maximum(abs(h - np.roll(c, 1)), abs(l - np.roll(c, 1))))
    tr[0] = h[0] - l[0]
    out = np.zeros_like(c)
    out[n - 1] = tr[:n].mean()
    for i in range(n, len(c)):
        out[i] = (out[i - 1] * (n - 1) + tr[i]) / n
    return out


def pivots(h, l, k=PIVOT):
    highs, lows = [], []
    for i in range(k, len(h) - k):
        if h[i] == h[i - k:i + k + 1].max():
            highs.append(i)
        if l[i] == l[i - k:i + k + 1].min():
            lows.append(i)
    return highs, lows


def levels(h, l, highs, lows, a, price, lookback=200):
    """Support / resistance = groups of swing points closer than 0.6 ATR, most touched first."""
    start = len(h) - lookback
    pts = [h[i] for i in highs if i >= start] + [l[i] for i in lows if i >= start]
    pts.sort()
    groups = []
    for p in pts:
        if groups and p - groups[-1][-1] <= 0.6 * a:
            groups[-1].append(p)
        else:
            groups.append([p])
    lv = [(float(np.mean(g)), len(g)) for g in groups]
    res = sorted([x for x in lv if x[0] > price * 1.001], key=lambda x: x[0])
    sup = sorted([x for x in lv if x[0] < price * 0.999], key=lambda x: -x[0])
    return sup, res


def divergence(c, h, l, r, highs, lows, n):
    """Most recent regular / hidden RSI divergence on the last two swing points (second one fresh)."""
    found = []
    for kind, pts, arr in (("low", lows, l), ("high", highs, h)):
        pts = [i for i in pts if i >= n - 80 and not np.isnan(r[i])]
        if len(pts) < 2:
            continue
        i1, i2 = pts[-2], pts[-1]
        if i2 - i1 < 8 or i2 < n - 25:
            continue
        p1, p2, r1, r2 = arr[i1], arr[i2], r[i1], r[i2]
        if kind == "low":
            if p2 < p1 and r2 > r1 + 2:
                found.append(("bullish", "regular", i1, i2))
            elif p2 > p1 and r2 < r1 - 2:
                found.append(("bullish", "hidden", i1, i2))
        else:
            if p2 > p1 and r2 < r1 - 2:
                found.append(("bearish", "regular", i1, i2))
            elif p2 < p1 and r2 > r1 + 2:
                found.append(("bearish", "hidden", i1, i2))
    if not found:
        return None
    side, kind, i1, i2 = max(found, key=lambda f: f[3])
    arr = l if side == "bullish" else h
    return {"side": side, "kind": kind, "i1": int(i1), "i2": int(i2), "price1": float(arr[i1]),
            "price2": float(arr[i2]), "rsi1": float(r[i1]), "rsi2": float(r[i2]), "bars_ago": int(n - 1 - i2)}


def candles(o, h, l, c, trend_down):
    """Patterns on the last 1-3 candles."""
    out = []
    body = abs(c - o)
    rng = np.maximum(h - l, 1e-12)
    up_w = h - np.maximum(o, c)
    lo_w = np.minimum(o, c) - l
    i = len(c) - 1
    if body[i] <= 0.1 * rng[i]:
        out.append(("Doji", 0, "noaniqlik: xaridorlar va sotuvchilar teng"))
    if lo_w[i] >= 2 * body[i] and up_w[i] <= 0.6 * body[i] + 0.1 * rng[i] and body[i] > 0.05 * rng[i]:
        out.append(("Hammer", 1, "pastdan kuchli qaytish") if trend_down else
                   ("Hanging man", -1, "o'sishdan keyin ogohlantirish"))
    if up_w[i] >= 2 * body[i] and lo_w[i] <= 0.6 * body[i] + 0.1 * rng[i] and body[i] > 0.05 * rng[i]:
        out.append(("Shooting star", -1, "yuqoridan kuchli bosim") if not trend_down else
                   ("Inverted hammer", 1, "pastda xaridorlar paydo bo'ldi"))
    if c[i - 1] < o[i - 1] and c[i] > o[i] and c[i] >= o[i - 1] and o[i] <= c[i - 1] and body[i] > body[i - 1]:
        out.append(("Bullish engulfing", 1, "xaridorlar oldingi shamni to'liq yutdi"))
    if c[i - 1] > o[i - 1] and c[i] < o[i] and c[i] <= o[i - 1] and o[i] >= c[i - 1] and body[i] > body[i - 1]:
        out.append(("Bearish engulfing", -1, "sotuvchilar oldingi shamni to'liq yutdi"))
    if (c[i - 2] < o[i - 2] and body[i - 1] < 0.4 * body[i - 2] and c[i] > o[i]
            and c[i] > (o[i - 2] + c[i - 2]) / 2):
        out.append(("Morning star", 1, "3 shamli pastdan burilish"))
    if (c[i - 2] > o[i - 2] and body[i - 1] < 0.4 * body[i - 2] and c[i] < o[i]
            and c[i] < (o[i - 2] + c[i - 2]) / 2):
        out.append(("Evening star", -1, "3 shamli yuqoridan burilish"))
    if h[i] < h[i - 1] and l[i] > l[i - 1]:
        out.append(("Inside bar", 0, "siqilish: yorib chiqish kutilmoqda"))
    if body[i] >= 0.85 * rng[i]:
        out.append(("Bullish marubozu", 1, "kuchli xarid shami") if c[i] > o[i] else
                   ("Bearish marubozu", -1, "kuchli sotish shami"))
    return [{"name": n, "score": s, "meaning": m} for n, s, m in out]


def r2(x):
    return float(f"{x:.4g}") if abs(x) < 1 else round(float(x), 2 if abs(x) >= 1 else 4)


def analyse(df, light=False):
    o, h, l, c, v = (df[k].to_numpy(dtype=float) for k in ("Open", "High", "Low", "Close", "Volume"))
    n = len(c)
    price = c[-1]
    e20, e50, e200 = ema(c, 20), ema(c, 50), ema(c, 200)
    rs, a = rsi(c), atr(h, l, c)
    A = float(a[-1]) or float(np.mean(h[-20:] - l[-20:]))
    highs, lows = pivots(h, l)
    sup, res = levels(h, l, highs, lows, A, price)

    slope = (e50[-1] - e50[-11]) / (10 * A) if n > 60 else 0
    if price > e20[-1] > e50[-1] and (n < 200 or e50[-1] > e200[-1]) and slope > 0:
        trend, tscore = "kuchli o'sish", 2
    elif price > e50[-1] and slope >= 0:
        trend, tscore = "o'sish", 1
    elif price < e20[-1] < e50[-1] and (n < 200 or e50[-1] < e200[-1]) and slope < 0:
        trend, tscore = "kuchli pasayish", -2
    elif price < e50[-1] and slope <= 0:
        trend, tscore = "pasayish", -1
    else:
        trend, tscore = "yon (flet)", 0
    hh = len(highs) >= 2 and h[highs[-1]] > h[highs[-2]]
    hl = len(lows) >= 2 and l[lows[-1]] > l[lows[-2]]
    structure = ("HH + HL (o'suvchi tuzilma)" if hh and hl else "LH + LL (pasayuvchi tuzilma)"
                 if not hh and not hl else "aralash tuzilma")

    div = divergence(c, h, l, rs, highs, lows, n)
    cnd = candles(o, h, l, c, tscore < 0)
    stp = setups(c, rs)
    r_now = float(rs[-1])
    vol_ratio = float(v[-1] / np.mean(v[-21:-1])) if v[-21:-1].mean() > 0 else None

    score = tscore
    if div:
        score += (1 if div["side"] == "bullish" else -1) * (2 if div["kind"] == "regular" else 1)
    score += sum(x["score"] for x in cnd)
    score += 1 if sum(x["score"] for x in stp) > 0 and tscore >= 0 else 0      # scanner setup with the trend
    if r_now >= 70:
        score -= 1
    elif r_now <= 30:
        score += 1
    bias = "LONG" if score >= 2 else "SHORT" if score <= -2 else "KUTISH"

    plan = make_plan(bias, price, A, sup, res, l, h, lows, highs)
    if bias == "KUTISH":
        plan["reason"] = ("trend o'suvchi, lekin RSI juda yuqori — pullback yoki yorib o'tishni kuting"
                          if tscore > 0 and r_now >= 65 else
                          "trend pasayuvchi, lekin RSI juda past — qaytish yoki yorib o'tishni kuting"
                          if tscore < 0 and r_now <= 35 else
                          "signallar bir-biriga zid — yorib o'tishni kuting" if tscore else
                          "aniq yo'nalish yo'q — diapazondan yorib chiqishni kuting")
    return {
        "price": r2(price), "atr": r2(A), "atr_pct": round(A / price * 100, 2),
        "ema20": r2(e20[-1]), "ema50": r2(e50[-1]), "ema200": r2(e200[-1]) if n >= 200 else None,
        "rsi": round(r_now, 1), "rsi_state": "overbought (70+)" if r_now >= 70 else "oversold (30-)"
        if r_now <= 30 else "neytral",
        "trend": trend, "structure": structure, "divergence": div, "candles": cnd, "setups": stp,
        "volume_confirmed": bool(vol_ratio and vol_ratio >= 1.5),
        "volume_vs_20avg": round(vol_ratio, 2) if vol_ratio else None,
        "supports": [{"price": r2(p), "touches": t} for p, t in sup[:3]],
        "resistances": [{"price": r2(p), "touches": t} for p, t in res[:3]],
        "score": score, "bias": bias, "plan": plan, "bars": n,
        "_series": {"o": o, "h": h, "l": l, "c": c, "e20": e20, "e50": e50, "rsi": rs, "index": df.index},
    }


def make_plan(bias, price, A, sup, res, l, h, lows, highs):
    def long_plan(entry, stop_ref):
        sl = stop_ref - 0.5 * A
        sl = min(max(sl, entry - 3.5 * A), entry - 1.0 * A)
        risk = entry - sl
        tps = [p for p, _ in res if p > entry + 0.8 * risk][:3]
        for mult in (2, 3, 4):
            if len(tps) >= 3:
                break
            t = entry + mult * risk
            if all(abs(t - x) > 0.5 * risk for x in tps):
                tps.append(t)
        tps = sorted(tps)[:3]
        return {"side": "LONG", "entry": r2(entry), "stop": r2(sl), "risk_pct": round(risk / entry * 100, 2),
                "targets": [{"price": r2(t), "rr": round((t - entry) / risk, 1)} for t in tps]}

    def short_plan(entry, stop_ref):
        sl = stop_ref + 0.5 * A
        sl = max(min(sl, entry + 3.5 * A), entry + 1.0 * A)
        risk = sl - entry
        tps = [p for p, _ in res[:0]]
        tps = [p for p, _ in sup if p < entry - 0.8 * risk][:3]
        for mult in (2, 3, 4):
            if len(tps) >= 3:
                break
            t = entry - mult * risk
            if t > 0 and all(abs(t - x) > 0.5 * risk for x in tps):
                tps.append(t)
        tps = sorted(tps, reverse=True)[:3]
        return {"side": "SHORT", "entry": r2(entry), "stop": r2(sl), "risk_pct": round(risk / entry * 100, 2),
                "targets": [{"price": r2(t), "rr": round((entry - t) / risk, 1)} for t in tps]}

    last_low = l[lows[-1]] if lows else l[-20:].min()
    last_high = h[highs[-1]] if highs else h[-20:].max()
    near_sup = next((p for p, _ in sup if p > price - 3.5 * A), last_low)
    near_res = next((p for p, _ in res if p < price + 3.5 * A), last_high)
    if bias == "LONG":
        p = long_plan(price, min(near_sup, last_low) if last_low < price else near_sup)
        p["zone"] = r2(near_sup) if near_sup < price else None
        return p
    if bias == "SHORT":
        p = short_plan(price, max(near_res, last_high) if last_high > price else near_res)
        p["zone"] = r2(near_res) if near_res > price else None
        return p
    up = res[0][0] if res else max(h[-20:].max(), price * 1.002)
    down = sup[0][0] if sup else min(l[-20:].min(), price * 0.998)
    return {"side": "KUTISH", "breakout_up": long_plan(up, up - 1.5 * A),
            "breakout_down": short_plan(down, down + 1.5 * A)}


# ----------------------------- Extras (ideas from day-trader-skill) ---------
# Ideas adapted from github.com/Jakeschincariol/day-trader-skill (MIT, (c) 2026 Jake Schincariol):
# scanner setups, the Time Machine (Monte Carlo replays + the same rule on past bars), the news /
# earnings guard, the risk gate and position sizing. Rewritten for Buffet AI's timeframes.

HORIZON = {"1m": 60, "3m": 40, "5m": 78, "15m": 26, "30m": 26, "1h": 35, "2h": 30, "4h": 30,
           "1D": 20, "1W": 12, "1M": 6}          # bars a plan has to work out (TP1 or stop)
REPLAYS, SEED = 5000, 19
COIN_FLIP, STRONG = 0.05, 0.15                    # verdict thresholds (points from break-even)
HISTORY_BARS = 1250                               # past bars the rule is re-run on (~5 years of daily bars)
MIN_TRADES = 15                                   # fewer independent trades = not enough to judge
ANALYSE_WINDOW = 400                              # bars each past signal sees (EMA 200 + levels)
BAR_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "2h": 120, "4h": 240,
               "1D": 1440, "1W": 10080, "1M": 43200}


def setups(c, rs):
    """Scanner setups on closes (trader-scanner's daily rules)."""
    out = []
    n = len(c)
    ma20 = np.convolve(c, np.ones(20) / 20, mode="valid") if n >= 21 else None
    if n >= 21 and c[-1] > c[-21:-1].max():
        out.append(("20 shamlik yopilish cho'qqisi yorib o'tildi", 1))
    if n >= 16:
        run = c[-6] / c[-16] - 1
        box = c[-5:].max() / c[-5:].min() - 1
        if run > 0.10 and box < 0.05:
            out.append(("Bull flag: kuchli o'sishdan keyin tor diapazon", 1))
    if ma20 is not None and len(ma20) >= 2 and c[-2] < ma20[-2] and c[-1] > ma20[-1]:
        out.append(("20 shamlik o'rtacha (MA20) qayta egallandi", 1))
    if not np.isnan(rs[-1]) and rs[-1] < 30:
        out.append(("Oversold: RSI 30 dan past", 0))
    if n >= 2 and not np.isnan(rs[-2]) and rs[-2] < 30 and c[-1] > c[-2]:
        out.append(("Oversold bounce: RSI 30 dan pastdan yuqoriga yopildi", 1))
    return [{"name": a, "score": b} for a, b in out]


def bar_pool(df, look=500):
    """Every bar as (open, high, low, close) over the previous close, from the recent past."""
    o, h, l, c = (df[k].to_numpy(dtype=float) for k in ("Open", "High", "Low", "Close"))
    prev = c[:-1]
    pool = np.stack([o[1:] / prev, h[1:] / prev, l[1:] / prev, c[1:] / prev], axis=1)
    pool = pool[np.all(np.isfinite(pool), axis=1)]
    return pool[-look:]


def simulate(pool, side, entry, stop, target, bars, replays=REPLAYS, seed=SEED):
    """Monte Carlo: rebuild the next `bars` bars from the stock's own past bars, `replays` times.
    Stop is checked before the target inside a bar (conservative)."""
    rng = np.random.default_rng(seed)
    long = side == "LONG"
    risk = (entry - stop) if long else (stop - entry)
    r_t = ((target - entry) if long else (entry - target)) / risk
    draws = pool[rng.integers(0, len(pool), size=(replays, bars))]
    closes = entry * np.cumprod(draws[:, :, 3], axis=1)
    prev = np.concatenate([np.full((replays, 1), entry), closes[:, :-1]], axis=1)
    hi, lo = prev * draws[:, :, 1], prev * draws[:, :, 2]
    hit_stop = lo <= stop if long else hi >= stop
    hit_tgt = hi >= target if long else lo <= target
    big = bars + 1
    first_stop = np.where(hit_stop.any(1), hit_stop.argmax(1), big)
    first_tgt = np.where(hit_tgt.any(1), hit_tgt.argmax(1), big)
    stop_first = (first_stop <= first_tgt) & (first_stop < big)
    tgt_first = (first_tgt < first_stop)
    neither = ~(stop_first | tgt_first)
    end = closes[:, -1]
    r_end = ((end - entry) if long else (entry - end)) / risk
    R = np.where(tgt_first, r_t, np.where(stop_first, -1.0, r_end))
    return {"replays": replays, "seed": seed, "bars": bars, "r_target": round(float(r_t), 2),
            "p_target": round(float(tgt_first.mean()), 4), "p_stop": round(float(stop_first.mean()), 4),
            "p_neither": round(float(neither.mean()), 4), "avg_R": round(float(R.mean()), 3),
            "p5": r2(float(np.percentile(end, 5))), "p50": r2(float(np.percentile(end, 50))),
            "p95": r2(float(np.percentile(end, 95)))}


def mc_verdict(p_target, p_stop, r_target):
    """trader-timemachine's verdict: the target's share of the replays that hit a side, against the
    share that breaks even at the target's R, 1 / (1 + R)."""
    if p_target + p_stop <= 0:
        return "neutral", "Hech bir replay TP1 yoki stop'ga yetmadi: natijani muddat oxiridagi narx hal qiladi"
    share, even = p_target / (p_target + p_stop), 1 / (1 + r_target)
    d = share - even
    if abs(d) < COIN_FLIP:
        return "neutral", f"Tanga tashlashga yaqin: TP1 {r_target:g}R da kamida {even * 100:.0f}% kerak, {share * 100:.0f}% chiqdi"
    if abs(d) < STRONG:
        return ("good" if d > 0 else "bad"), (f"TP1 tomonga og'adi ({share * 100:.0f}% vs zarar-siz {even * 100:.0f}%)"
                                               if d > 0 else f"Stop tomonga og'adi ({share * 100:.0f}% vs zarar-siz {even * 100:.0f}%)")
    return ("good" if d > 0 else "bad"), (f"Kuchli TP1 tomonga ({share * 100:.0f}% vs zarar-siz {even * 100:.0f}%)"
                                           if d > 0 else f"Kuchli stop tomonga ({share * 100:.0f}% vs zarar-siz {even * 100:.0f}%)")


def replay_signals(df, side, bars, start=260, end=None, gate=None, deadline=None, window=ANALYSE_WINDOW,
                   exit_fn=None):
    """Re-run the Buffet AI rule bar by bar on the past and play each signal's own plan (entry, stop, TP1)
    forward on the real bars that followed. Trades never overlap: after a signal the next one is looked for
    only after this trade has ended (TP1, stop, or `bars` bars later). Each bar sees only the past
    (the last `window` bars, enough for EMA 200, levels and divergences)."""
    o_all, h_all, l_all, c_all = (df[k].to_numpy(dtype=float) for k in ("Open", "High", "Low", "Close"))
    n = len(df)
    end = n - 1 if end is None else end
    trades = []
    t = max(start, 60)
    while t < end:
        if deadline and time.time() > deadline:
            break
        a = analyse(df.iloc[max(0, t + 1 - window):t + 1], light=True)
        p = a["plan"]
        if p["side"] != side or not p.get("targets") or (gate and not gate(a, df.iloc[:t + 1])):
            t += 1
            continue
        e, st, tg = exit_fn(a) if exit_fn else (p["entry"], p["stop"], p["targets"][0]["price"])
        risk = abs(e - st)
        out, result, last_j = None, "neither", min(t + bars, n - 1)
        for j in range(t + 1, last_j + 1):
            if side == "LONG":
                if l_all[j] <= st:                     # stop first when both are inside one bar
                    out, result = (min(o_all[j], st) - e) / risk, "stop"     # a gap below the stop fills worse
                elif h_all[j] >= tg:
                    out, result = (tg - e) / risk, "target"
            else:
                if h_all[j] >= st:
                    out, result = (e - max(o_all[j], st)) / risk, "stop"
                elif l_all[j] <= tg:
                    out, result = (e - tg) / risk, "target"
            if out is not None:
                last_j = j
                break
        if out is None:
            if t + bars > n - 1:                       # not finished yet: not a trade
                break
            out = ((c_all[last_j] - e) if side == "LONG" else (e - c_all[last_j])) / risk
        trades.append({"date": str(df.index[t].date()), "R": round(float(out), 3), "result": result,
                       "score": a["score"], "trend": a["trend"], "rr": p["targets"][0]["rr"],
                       "setups": [x["name"] for x in a["setups"]], "rsi": a["rsi"]})
        t = last_j + 1                                 # the next trade starts after this one ended
    return trades


def summarize(trades):
    if not trades:
        return {"trades": 0, "target": 0, "stop": 0, "neither": 0, "avg_R": None, "win_pct": None}
    tg = sum(1 for x in trades if x["result"] == "target")
    st = sum(1 for x in trades if x["result"] == "stop")
    return {"trades": len(trades), "target": tg, "stop": st, "neither": len(trades) - tg - st,
            "avg_R": round(float(np.mean([x["R"] for x in trades])), 3),
            "win_pct": round(tg / len(trades) * 100, 1)}


def history_test(df, side, bars, max_tests=HISTORY_BARS, deadline=None, exit_fn=None):
    """The same rule on this instrument's own past (independent trades only). A result counts only
    with at least MIN_TRADES trades; fewer = 'ma'lumot kam'. exit_fn: the exit the current plan uses."""
    n = len(df)
    trades = replay_signals(df, side, bars, start=max(260, n - max_tests - bars), deadline=deadline,
                            exit_fn=exit_fn)
    s = summarize(trades)
    s.update(fired=s["trades"], tested_bars=min(max_tests, n - 260), enough=s["trades"] >= MIN_TRADES,
             avg_R=round(s["avg_R"], 2) if s["avg_R"] is not None else None)
    return s


_CAL = {}


def load_calibration(path="calibration.json"):
    """Weekly market-wide statistics made by calibrate.py (None if the file is not there yet)."""
    if "data" not in _CAL:
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), path), encoding="utf-8") as f:
                _CAL["data"] = json.load(f)
        except Exception:
            _CAL["data"] = None
    return _CAL["data"]


def calibration_group(cal, a):
    """The most specific market-wide group for this LONG signal (Buffet AI's own exit, all 5 years):
    its setup, else its score, else all."""
    if not cal:
        return None
    g = cal.get("groups", {})

    def pick(key):
        v = g.get(key)
        if not v:
            return None
        st = v.get("A", {}).get("all") if cal.get("version", 1) >= 2 else v
        return dict(st, group=key) if st and st.get("trades") else None

    for st in a.get("setups") or []:
        r = pick(f"setap: {st['name']}")
        if r:
            return r
    sc = a["score"]
    keys = (["ball +2"] if sc <= 2 else ["ball +3"] if sc == 3 else ["ball +4+", "ball +4 va yuqori"]) + ["all"]
    for key in keys:
        r = pick(key)
        if r:
            return r
    return None


def events(ysym, tf, df):
    """News guard: next earnings date inside the plan's horizon, and today's move."""
    out = {"earnings": None, "earnings_in_window": False, "move_pct": None}
    try:
        c = df["Close"].to_numpy(dtype=float)
        out["move_pct"] = round(float(c[-1] / c[-2] - 1) * 100, 2)
    except Exception:
        pass
    if "=" in ysym or ysym.startswith("^") or ysym.endswith("-USD"):
        return out                                        # no earnings for fx, futures, indices, crypto
    try:
        import yfinance as yf
        cal = yf.Ticker(ysym).calendar or {}
        dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if dates:
            d = min(dates)
            if isinstance(d, datetime):
                d = d.date()
            out["earnings"] = d.isoformat()
            days = (d - datetime.now(timezone.utc).date()).days
            m = BAR_MINUTES[tf]
            trading_days = HORIZON[tf] * m / 390 if m < 1440 else HORIZON[tf] * {1440: 1, 10080: 5, 43200: 21}[m]
            horizon_days = trading_days * 7 / 5
            out["earnings_days"] = days
            out["earnings_in_window"] = 0 <= days <= max(1, round(horizon_days))
    except Exception as exc:
        print(f"calendar {ysym}: {exc}")
    return out


def risk_gate(plan, a, df, ev, asset):
    """trader-risk style checks on the plan. Returns a list of (level, text); level 'block' or 'warn'."""
    notes = []
    sides = [plan] if plan["side"] != "KUTISH" else [plan["breakout_up"], plan["breakout_down"]]
    for p in sides:
        if p.get("targets") and p["targets"][0]["rr"] < 1.0:
            notes.append(("warn", f"{p['side']}: TP1 risk/reward 1:{p['targets'][0]['rr']} — 1:1 dan past"))
        if p["risk_pct"] > 10:
            notes.append(("warn", f"{p['side']}: stop narxdan {p['risk_pct']}% uzoq — pozitsiyani kichik tuting"))
    try:
        v = df["Volume"].to_numpy(dtype=float)[-20:]
        c = df["Close"].to_numpy(dtype=float)[-20:]
        dollar = float(np.mean(v * c))
        if asset not in ("forex", "index", "futures") and 0 < dollar < 5e6 and a["bars"] and v.sum() > 0:
            notes.append(("warn", f"Likvidlik past: o'rtacha shamda ~${dollar / 1e6:.1f}M savdo — spred va slippage katta bo'lishi mumkin"))
    except Exception:
        pass
    if a["price"] < 5 and asset in ("stock", "etf", ""):
        notes.append(("warn", "Narx $5 dan past (penny aksiya): tebranish va manipulyatsiya xavfi yuqori"))
    if ev.get("earnings_in_window"):
        notes.append(("block", f"Hisobot {ev['earnings']} da — reja muddati ichida. Hisobotdagi gap stop'dan o'tib ketishi mumkin"))
    if ev.get("move_pct") is not None and abs(ev["move_pct"]) >= 3:
        notes.append(("warn", f"Oxirgi shamda {ev['move_pct']:+.1f}% harakat — sababini News bo'limida tekshiring"))
    return notes


def sizing(plan, deposit=1000.0, risk_pct=1.0, fractional=False):
    """trader-budget: shares that lose `risk_pct` of the deposit at the stop, capped by the deposit."""
    if plan["side"] == "KUTISH":
        return None
    risk_share = abs(plan["entry"] - plan["stop"])
    if risk_share <= 0:
        return None
    qty = deposit * risk_pct / 100 / risk_share
    qty = min(qty, deposit / plan["entry"])
    qty = round(qty, 4) if fractional else int(qty)
    return {"deposit": deposit, "risk_pct": risk_pct, "qty": qty, "cost": round(qty * plan["entry"], 2),
            "max_loss": round(qty * risk_share, 2)}


def extras(df, a, meta, ysym, deadline):
    """Everything above for the current plan."""
    tf = meta["tf"]
    bars = HORIZON.get(tf, 20)
    p = a["plan"]
    out = {"horizon_bars": bars}
    pool = bar_pool(df)
    if p["side"] != "KUTISH" and p.get("targets") and len(pool) >= 60:
        mc = simulate(pool, p["side"], p["entry"], p["stop"], p["targets"][0]["price"], bars)
        mc["verdict_kind"], mc["verdict"] = mc_verdict(mc["p_target"], mc["p_stop"], mc["r_target"])
        out["mc"] = mc
        evd = a.get("evidence") or {}
        exit_fn = None
        if evd.get("variant"):                     # the history must use the same exit as the plan shown
            import rules as R
            exit_fn = lambda x, v=evd["variant"]: R.variant_plan(v, x)
        if len(df) >= bars + 300:
            out["history"] = history_test(df, p["side"], bars, deadline=deadline, exit_fn=exit_fn)
        if tf == "1D" and p["side"] == "LONG" and not evd:
            out["calibration"] = calibration_group(load_calibration(), a)
    ev = events(ysym, tf, df)
    out["events"] = ev
    out["risk"] = risk_gate(p, a, df, ev, meta.get("asset", ""))
    out["size"] = sizing(p, fractional=ysym.endswith("-USD"))
    return out


# ----------------------------- Formatting -----------------------------------

def esc(s):
    return html.escape(str(s or ""), quote=False)


def fp(x):
    if x is None:
        return "—"
    return f"${x:,.2f}" if abs(x) >= 1 else f"${x:.4g}"


def plan_lines(p):
    lines = [f"   Kirish: <b>{fp(p['entry'])}</b>",
             f"   🛑 Stop-loss: <b>{fp(p['stop'])}</b> (risk {p['risk_pct']}%)"]
    for k, t in enumerate(p["targets"], 1):
        lines.append(f"   🎯 TP{k}: <b>{fp(t['price'])}</b> · R/R 1:{t['rr']}")
    return lines


# ----------------------------- 5-year evidence (calibration v2) -------------

def evidence(a, df, meta):
    """Daily charts only: does this signal match a rule that passed BOTH 5-year test periods?
    LONG + approved rule  -> the rule's own exit (stop / take profit).
    LONG + no rule        -> 'not proven': the best tested exit (C: 2 ATR stop, 2R) and a clear warning.
    SHORT                 -> SHORT signals are not tested yet (said honestly).
    KUTISH                -> no LONG signal now: the 5-year facts of the current context are still shown."""
    if meta.get("tf") != "1D":
        return None
    cal = load_calibration()
    if not cal or cal.get("version", 1) < 2:
        return None
    import rules as R                                     # lazy: rules.py imports this file
    if a["plan"]["side"] == "SHORT":
        return {"status": "short"}
    if a["plan"]["side"] == "KUTISH":
        return {"status": "no_signal", "facts": context_facts(a, df, cal, R)}
    spy = load_prices("SPY", "1D")
    ctx = R.spy_context(spy) if spy is not None else {}
    f = R.features(a, df.iloc[-501:], ctx)
    groups = cal.get("groups", {})
    facts = []
    for key in (f"bozor: {f['bozor']}", f"kuch: {f['kuch']}", f"rsi: {f['rsi']}", f"trend: {f['trend']}"):
        g = (groups.get(key) or {}).get("C")
        if g and g["all"]["trades"]:
            facts.append({"label": R.human(key), "trades": g["all"]["trades"], "avg_R": g["all"]["avg_R"],
                          "disc": g["disc"]["avg_R"], "val": g["val"]["avg_R"]})
    approved = [r for r in cal.get("approved", []) if r["val"]["trades"] >= R.MIN_VAL_TRADES]
    matches = [r for r in approved if R.rule_matches(r["rule"], f)] if R.basic_gate(a) else []
    rule = max(matches, key=lambda r: (r["disc"]["avg_R"] + r["val"]["avg_R"]) / 2) if matches else None
    v = rule["variant"] if rule else "C"
    e, st, tp = R.variant_plan(v, a)
    risk = e - st
    a["plan"] = {"side": "LONG", "entry": r2(e), "stop": r2(st), "risk_pct": round(risk / e * 100, 2),
                 "targets": [{"price": r2(tp), "rr": round((tp - e) / risk, 1)},
                             {"price": r2(e + 2 * (tp - e)), "rr": round(2 * (tp - e) / risk, 1)}],
                 "zone": a["plan"].get("zone")}
    return {"status": "approved" if rule else "not_proven", "rule": rule,
            "rule_text": R.human(rule["rule"]) if rule else None, "exit": R.VARIANTS[v], "variant": v,
            "facts": facts, "regime": f["bozor"], "updated": cal.get("updated", "")[:10]}


def context_facts(a, df, cal, R):
    """5-year results of the market regime, relative strength, RSI zone and trend of this chart now
    (exit C), so the evidence is visible even when there is no LONG signal."""
    spy = load_prices("SPY", "1D")
    ctx = R.spy_context(spy) if spy is not None else {}
    c = df["Close"].to_numpy(dtype=float)
    above, spy63 = ctx.get(df.index[-1].date(), (None, None))
    if above is None and ctx:
        above, spy63 = ctx[max(ctx)]
    rs = (c[-1] / c[-64] - 1 - spy63) if (len(c) > 64 and spy63 is not None) else None
    rsi = a["rsi"]
    keys = []
    if above is not None:
        keys.append(f"bozor: {'SPY>MA200' if above else 'SPY<MA200'}")
    if rs is not None:
        keys.append(f"kuch: {'RS<0' if rs < 0 else 'RS 0-10%' if rs < 0.10 else 'RS>10%'}")
    keys.append("rsi: " + ("RSI<50" if rsi < 50 else "RSI 50-60" if rsi < 60 else "RSI 60-70" if rsi < 70
                           else "RSI 70-80"))
    keys.append(f"trend: {a['trend']}")
    out = []
    for key in keys:
        g = (cal.get("groups", {}).get(key) or {}).get("C")
        if g and g["all"]["trades"]:
            out.append({"label": R.human(key), "trades": g["all"]["trades"], "avg_R": g["all"]["avg_R"],
                        "disc": g["disc"]["avg_R"], "val": g["val"]["avg_R"]})
    return out


def evidence_lines(a):
    e = a.get("evidence")
    if not e:
        return []
    if e["status"] == "short":
        return ["📚 <b>5 yillik dalil:</b> SHORT signallar hali 5 yillik sinovdan o'tkazilmagan — ehtiyot bo'ling.", ""]
    out = ["📚 <b>5 yillik dalil</b> (957 aksiya, komissiyadan keyin)"]
    if e["status"] == "no_signal":
        out.append("   ℹ️ Hozir LONG signal yo'q, shuning uchun tasdiqlangan qoidalar qo'llanmaydi. Yorib o'tish "
                   "bo'lib, Buffet AI LONG desa, signal qoidalar bilan qayta tekshiriladi. Shunday holatdagi LONG signallarning 5 yillik natijasi:")
    elif e["status"] == "approved":
        r = e["rule"]
        out += [f"   ✅ Tasdiqlangan qoida: {esc(e['rule_text'])}",
                f"   2021–24: {r['disc']['trades']} savdo, {r['disc']['avg_R']:+.2f}R · "
                f"2025–26: {r['val']['trades']} savdo, {r['val']['avg_R']:+.2f}R"]
    elif e["status"] == "not_proven":
        out += ["   ⚠️ <b>Tasdiqlanmagan signal:</b> bunday LONG signallar 5 yillik sinovda barqaror foyda "
                "bermagan. Kichik pozitsiya yoki kutish tavsiya etiladi."]
    for x in e["facts"]:
        icon = "🟢" if x["disc"] > 0 and x["val"] > 0 else "🔴" if x["disc"] < 0 and x["val"] < 0 else "🟡"
        out.append(f"   {icon} {esc(x['label'])}: {x['trades']:,} savdo, 2021–24 {x['disc']:+.2f}R · "
                   f"2025–26 {x['val']:+.2f}R".replace(",", " ", 1))
    if e.get("exit"):
        out.append(f"   🚪 Chiqish: {esc(e['exit'])} — 5 yilda eng yaxshi natija bergan variant")
    out.append("")
    return out


def extras_lines(a):
    x = a.get("extras") or {}
    out = []
    mc = x.get("mc")
    if mc:
        icon = {"good": "🟢", "bad": "🔴", "neutral": "⚖️"}[mc["verdict_kind"]]
        reps = f"{mc['replays']:,}".replace(",", " ")
        out += [f"🎲 <b>Time Machine</b> ({reps} ta replay, keyingi {mc['bars']} sham)",
                f"   TP1 birinchi: <b>{mc['p_target'] * 100:.1f}%</b> · stop birinchi: <b>{mc['p_stop'] * 100:.1f}%</b>"
                f" · hech biri: {mc['p_neither'] * 100:.1f}%",
                f"   O'rtacha natija: {mc['avg_R']:+.2f}R · muddat oxiridagi narx: {fp(mc['p5'])} – {fp(mc['p95'])}"
                f" (90%), mediana {fp(mc['p50'])}",
                f"   {icon} {esc(mc['verdict'])}"]
        if mc["p_neither"] > 0.4:
            out.append(f"   ℹ️ Replaylarning {mc['p_neither'] * 100:.0f}% i {mc['bars']} shamda TP1 ham, stop ham "
                       "urmagan — ularni muddat oxiridagi narx hal qiladi")
        if mc["verdict_kind"] in ("good", "bad") and (mc["avg_R"] > 0) != (mc["verdict_kind"] == "good") \
                and mc["avg_R"] != 0:
            out.append(f"   ℹ️ Shunga qaramay o'rtacha natija {mc['avg_R']:+.2f}R: buni hech biriga yetmagan replaylar "
                       "teskari tomonga tortadi")
        h = x.get("history")
        if h and h["fired"]:
            years = max(1, round(h["tested_bars"] / 252))
            same = ", xuddi shu stop/TP bilan" if (a.get("evidence") or {}).get("variant") else ""
            line = (f"   📜 Shu aksiyada o'tmishda (~{years} yil{same}) {h['fired']} ta mustaqil savdo: "
                    f"{h['target']} TP1, {h['stop']} stop, {h['neither']} hech biri"
                    + (f" · o'rtacha {h['avg_R']:+.2f}R" if h["avg_R"] is not None else ""))
            if not h.get("enough", True):
                line += f" — <i>ma'lumot kam ({MIN_TRADES} tadan kam), xulosa uchun yetarli emas</i>"
            out.append(line)
        elif h:
            out.append(f"   📜 Shu qoida oxirgi {h['tested_bars']} shamda bu signalni bermagan")
        cal = x.get("calibration")
        if cal:
            n_tr = f"{cal['trades']:,}".replace(",", " ")
            out.append(f"   📊 Butun bozor (5 yil, skaner signallari, {esc(cal['group'])}): {n_tr} ta → TP1 {cal['win_pct']:.0f}% · "
                       f"stop {cal['stop'] / cal['trades'] * 100:.0f}% · o'rtacha {cal['avg_R']:+.2f}R")
        out += ["   <i>Simulyatsiya — bashorat emas: kelajakda o'tmishda bo'lmagan voqealar ham bo'lishi mumkin.</i>", ""]
    st = a.get("setups") or []
    if st:
        out += ["🔎 <b>Skaner setaplari</b>"] + [f"   • {esc(s_['name'])}" for s_ in st] + [""]
    if a.get("volume_confirmed"):
        out += ["📊 Oxirgi sham hajmi o'rtachadan 1,5 barobar yuqori — harakat hajm bilan tasdiqlangan", ""]
    risk = x.get("risk") or []
    if risk:
        out += ["🛡 <b>Risk tekshiruvi</b>"] + [f"   {'⛔' if lv == 'block' else '⚠️'} {esc(t)}" for lv, t in risk] + [""]
    elif x:
        out += ["🛡 <b>Risk tekshiruvi:</b> ✅ jiddiy ogohlantirish yo'q", ""]
    sz = x.get("size")
    if sz and sz["qty"]:
        out += [f"💼 <b>Pozitsiya hajmi</b> (misol: ${sz['deposit']:,.0f} depozit, {sz['risk_pct']:g}% risk)",
                f"   {sz['qty']:g} ta ≈ ${sz['cost']:,.2f} · stop urilsa zarar ≈ ${sz['max_loss']:,.2f}",
                "   <i>Formula: (depozit × risk %) ÷ (kirish − stop)</i>", ""]
    return out


def format_text(meta, a, ex, image_only):
    head = f"🧠 <b>Buffet AI · {esc(meta['title'])}</b>"
    sub = f"<i>{esc(TF_NAMES.get(meta['tf'], meta['tf']))} grafik"
    if a:
        sub += f" · narx {fp(a['price'])} · RSI {a['rsi']} · ATR {fp(a['atr'])} ({a['atr_pct']}%)"
    lines = [head, sub + "</i>", ""]
    if image_only:
        lines += ["ℹ️ <i>Bu tiker uchun narx ma'lumoti topilmadi, tahlil faqat rasm asosida. "
                  "Darajalar rasmdan o'qilgan va taxminiy.</i>", ""]
    for icon, title, key in (("📈", "Trend", "trend"), ("🕯", "Shamlar", "candles"),
                             ("📐", "Formatsiyalar", "patterns"), ("📉", "RSI va divergensiya", "rsi"),
                             ("📊", "Hajm", "volume")):
        if ex.get(key):
            lines += [f"{icon} <b>{title}</b>", esc(ex[key]), ""]
    if a:
        p = a["plan"]
        if p["side"] == "KUTISH":
            lines += [f"⏸ <b>Savdo rejasi: KUTISH</b> — {esc(p.get('reason', ''))}", "",
                      f"⬆️ <b>Yuqoriga yorib o'tsa</b> ({fp(p['breakout_up']['entry'])} dan yuqorida yopilsa):"]
            lines += plan_lines(p["breakout_up"]) + [""]
            lines += [f"⬇️ <b>Pastga yorib o'tsa</b> ({fp(p['breakout_down']['entry'])} dan pastda yopilsa):"]
            lines += plan_lines(p["breakout_down"])
        else:
            icon = "🟢" if p["side"] == "LONG" else "🔴"
            lines += [f"{icon} <b>Savdo rejasi: {p['side']}</b>"] + plan_lines(p)
            if p.get("zone"):
                word = "pullback" if p["side"] == "LONG" else "qaytish"
                lines.append(f"   💡 Yaxshiroq kirish ({word}): {fp(p['zone'])} atrofi")
        lines.append("")
        lines += evidence_lines(a) + extras_lines(a)
        lv = " · ".join(fp(x["price"]) for x in a["supports"]) or "—"
        lr = " · ".join(fp(x["price"]) for x in a["resistances"]) or "— (narx eng yuqori nuqtalar yaqinida)"
        lines += [f"🧱 Qo'llab-quvvatlash: {lv}", f"🧱 Qarshilik: {lr}", ""]
    elif ex.get("levels"):
        lines += [f"🎯 <b>Taxminiy darajalar (rasmdan)</b>", esc(ex["levels"]), ""]
    if ex.get("plan_comment"):
        lines += ["🧭 <b>Nega aynan shu darajalar</b>", esc(ex["plan_comment"]), ""]
    if ex.get("summary"):
        lines += [f"✅ <b>Xulosa:</b> {esc(ex['summary'])}", ""]
    lines.append(f"<i>{esc(DISCLAIMER)}</i>")
    return "\n".join(lines)


# ----------------------------- Chart ----------------------------------------

THEME = {"bg": "#0b1220", "panel": "#111a2e", "line": "#1e2a44", "ink": "#e6edf7", "muted": "#8a99b8",
         "up": "#22c55e", "down": "#ef4444", "e20": "#38bdf8", "e50": "#f59e0b", "entry": "#e6edf7",
         "tp": "#22c55e", "sl": "#ef4444", "wait": "#94a3b8", "rsi": "#a78bfa"}


def _font():
    import glob
    from matplotlib import font_manager
    for path in glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", "*.[ot]tf")):
        try:
            font_manager.fontManager.addfont(path)
        except Exception as exc:
            print(f"Font {path}: {exc}")
    names = {f.name for f in font_manager.fontManager.ttflist}
    return "Inter" if "Inter" in names else "DejaVu Sans"


def make_chart(meta, a):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    T = THEME
    plt.rcParams["font.family"] = _font()
    s = a["_series"]
    N = min(BARS_ON_CARD, len(s["c"]))
    off = len(s["c"]) - N
    o, h, l, c = (s[k][-N:] for k in "ohlc")
    xs = np.arange(N)
    fig = plt.figure(figsize=(10, 8.6), dpi=160, facecolor=T["bg"])
    head = fig.add_axes([0, 0.86, 1, 0.14])
    head.set_axis_off()
    head.set_xlim(0, 1)
    head.set_ylim(0, 1)
    head.text(0.04, 0.62, meta["title"], fontsize=24, fontweight="bold", color=T["ink"], va="center")
    head.text(0.04, 0.24, f"{TF_NAMES.get(meta['tf'], meta['tf'])} · narx {fp(a['price'])} · RSI {a['rsi']} · "
                          f"trend: {a['trend']}", fontsize=10, color=T["muted"], va="center")
    p = a["plan"]
    mc = (a.get("extras") or {}).get("mc")
    if mc:
        head.text(0.955, 0.12, f"Time Machine: TP1 {mc['p_target'] * 100:.0f}% · stop {mc['p_stop'] * 100:.0f}%",
                  fontsize=9, color=T["muted"], va="center", ha="right")
    color = {"LONG": T["up"], "SHORT": T["down"], "KUTISH": T["wait"]}[p["side"]]
    ev = a.get("evidence") or {}
    pill = p["side"] + (" ✓" if ev.get("status") == "approved" else " ?" if ev.get("status") == "not_proven" else "")
    head.text(0.955, 0.55, pill, fontsize=15, fontweight="bold", color=color, va="center", ha="right",
              bbox=dict(boxstyle="round,pad=0.45,rounding_size=0.6", facecolor=T["panel"], edgecolor=color,
                        linewidth=1.4))

    ax = fig.add_axes([0.06, 0.33, 0.78, 0.52], facecolor=T["bg"])
    rx = fig.add_axes([0.06, 0.09, 0.78, 0.2], facecolor=T["bg"], sharex=ax)
    for k in range(N):
        col = T["up"] if c[k] >= o[k] else T["down"]
        ax.vlines(k, l[k], h[k], color=col, linewidth=0.8)
        ax.add_patch(plt.Rectangle((k - 0.33, min(o[k], c[k])), 0.66, max(abs(c[k] - o[k]), 1e-9), color=col))
    ax.plot(xs, s["e20"][-N:], color=T["e20"], linewidth=1.1, label="EMA 20")
    ax.plot(xs, s["e50"][-N:], color=T["e50"], linewidth=1.1, label="EMA 50")

    lines = []
    if p["side"] == "KUTISH":
        bu, bd = p["breakout_up"], p["breakout_down"]
        lines += [(bu["entry"], T["up"], "Yorib o'tish ↑"), (bd["entry"], T["down"], "Yorib o'tish ↓")]
        if bu["targets"]:
            lines.append((bu["targets"][0]["price"], T["tp"], "TP1 ↑"))
        if bd["targets"]:
            lines.append((bd["targets"][0]["price"], T["tp"], "TP1 ↓"))
    else:
        lines.append((p["entry"], T["entry"], "Kirish"))
        lines.append((p["stop"], T["sl"], "Stop-loss"))
        for k, t in enumerate(p["targets"], 1):
            lines.append((t["price"], T["tp"], f"TP{k}"))
    lo, hi = l.min(), h.max()
    for y, col, _ in lines:
        lo, hi = min(lo, y), max(hi, y)
    pad = (hi - lo) * 0.06
    ax.set_ylim(lo - pad, hi + pad)
    for y, col, label in lines:
        ax.axhline(y, color=col, linewidth=1.1, linestyle="--" if label != "Kirish" else "-", alpha=0.9)
        ax.text(N + 1.5, y, f"{label} {fp(y)}", color=col, fontsize=8.5, va="center", ha="left",
                fontweight="bold", clip_on=False)
    for lv in a["supports"][:2] + a["resistances"][:2]:
        ax.axhspan(lv["price"] - a["atr"] * 0.12, lv["price"] + a["atr"] * 0.12, color=T["muted"], alpha=0.12)

    r = s["rsi"][-N:]
    rx.plot(xs, r, color=T["rsi"], linewidth=1.2)
    for y in (30, 70):
        rx.axhline(y, color=T["line"], linewidth=1, linestyle="--")
    rx.set_ylim(0, 100)
    rx.text(1, 88, f"RSI {RSI_LEN}: {a['rsi']}", color=T["rsi"], fontsize=9)

    d = a["divergence"]
    if d and d["i1"] >= off:
        i1, i2 = d["i1"] - off, d["i2"] - off
        col = T["up"] if d["side"] == "bullish" else T["down"]
        ax.plot([i1, i2], [d["price1"], d["price2"]], color=col, linewidth=2)
        rx.plot([i1, i2], [d["rsi1"], d["rsi2"]], color=col, linewidth=2)
        word = ("Bullish" if d["side"] == "bullish" else "Bearish") + (" divergensiya" if d["kind"] == "regular"
                                                                         else " yashirin divergensiya")
        rx.text((i1 + i2) / 2, max(d["rsi1"], d["rsi2"]) + 9 if d["side"] == "bearish"
                else min(d["rsi1"], d["rsi2"]) - 16, word, color=col, fontsize=8.5, ha="center", fontweight="bold")

    idx = s["index"][-N:]
    ticks = list(range(0, N, max(N // 6, 1)))
    fmt = "%d.%m" if meta["tf"] in ("1D", "1W", "1M") else "%d.%m %H:%M"
    if meta["tf"] in ("1W", "1M"):
        fmt = "%m.%Y"
    rx.set_xticks(ticks, [idx[t].strftime(fmt) for t in ticks])
    for axx in (ax, rx):
        axx.tick_params(colors=T["muted"], labelsize=8.5, length=0)
        axx.grid(color=T["line"], linewidth=0.6)
        for sp in axx.spines.values():
            sp.set_visible(False)
        axx.yaxis.tick_left()
    plt.setp(ax.get_xticklabels(), visible=False)
    ax.set_xlim(-1, N)
    leg = ax.legend(loc="upper left", fontsize=8, frameon=False)
    for t in leg.get_texts():
        t.set_color(T["muted"])
    fig.text(0.04, 0.025, "Buffet AI · Trading Laboratory · Warren Buffett bilan bog'liq emas", fontsize=8,
             color=T["muted"])
    fig.text(0.96, 0.025, "Investitsion maslahat emas", fontsize=8, color=T["muted"], ha="right")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=T["bg"])
    plt.close(fig)
    return buf.getvalue()


# ----------------------------- Main -----------------------------------------

def public(a):
    """Data for Gemini without the raw series."""
    out = {k: v for k, v in a.items() if not k.startswith("_")}
    if out.get("divergence"):
        out["divergence"] = {k: v for k, v in out["divergence"].items() if k not in ("i1", "i2")}
    return out


def main():
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY secret is missing")
    image, mime = (download_photo(FILE_ID) if FILE_ID else (None, None))
    cap = CAPTION
    words = cap.split()
    cap_symbol = words[0].lstrip("$") if words and words[0].startswith("$") else ""
    cap_tf = words[1] if cap_symbol and len(words) > 1 and re.fullmatch(r"\d{0,3}[a-zA-Z]{1,3}|\d{1,3}", words[1]) \
        else ""
    meta = {"symbol": "", "asset": "", "tf": "1D", "read": {}}
    if image:
        read = gemini_json([{"text": READ_PROMPT % cap.replace('"', "'")},
                            {"inline_data": {"mime_type": mime, "data": base64.b64encode(image).decode()}}], "read")
        print("read:", json.dumps(read, ensure_ascii=False))
        if read.get("is_chart") is False:
            send_text("🤔 Bu rasmda savdo grafigi ko'rinmadi. WeBull yoki TradingView'dan grafik skrinshotini "
                      "yuboring (tiker va vaqt oralig'i ko'rinib tursin).")
            return
        meta.update(symbol=read.get("symbol") or "", asset=read.get("asset") or "",
                    tf=norm_tf(read.get("timeframe") or "1D"), read=read)
    if cap_symbol:                                       # caption / text wins: "$TSLA 4h"
        meta["symbol"] = cap_symbol
        if cap_tf:
            meta["tf"] = norm_tf(cap_tf)
    if not image and not meta["symbol"]:
        send_text("❌ Grafik rasmini yuboring yoki tikerni yozing, masalan: $TSLA yoki $TSLA 4h")
        return

    ysym = yahoo_symbol(meta["symbol"], meta["asset"])
    meta["title"] = f"${re.sub(r'^[A-Z]+:', '', meta['symbol'].upper())}" if meta["symbol"] else "Grafik"
    a = None
    if ysym:
        try:
            df = load_prices(ysym, meta["tf"])
            if df is not None:
                a = analyse(df)
                seen = meta["read"].get("last_price")
                if seen and a["price"] and abs(seen / a["price"] - 1) > 0.25:
                    print(f"price mismatch: picture {seen} vs data {a['price']} -> picture only")
                    a = None
        except Exception as exc:
            print(f"prices {ysym}: {exc}")
            a = None
    if a:
        try:
            a["evidence"] = evidence(a, df, meta)
        except Exception as exc:
            print(f"evidence: {exc}")
            a["evidence"] = None
        try:
            a["extras"] = extras(df, a, meta, ysym, deadline=time.time() + 45)
        except Exception as exc:
            print(f"extras: {exc}")
            a["extras"] = {}
    print(f"{ysym} {meta['tf']}: {'data' if a else 'picture only'}")
    if not a and not image:
        send_text(f"❌ {meta['title']} uchun narx ma'lumoti topilmadi. Tikerni tekshiring yoki grafik rasmini "
                  "yuboring.")
        return

    if a:
        intro = (f"Instrument: {meta['title']} ({ysym}), timeframe {meta['tf']}. "
                 + ("The user's chart picture is attached." if image else "No picture: use only the data."))
        data = json.dumps(public(a), ensure_ascii=False)
    else:
        intro = ("Only the picture is available (no price data). Read prices from the right axis. Add a field "
                 "\"levels\": approximate entry, stop-loss and 2-3 take profits read from the picture, clearly "
                 "marked as approximate. Picture facts: " + json.dumps(meta["read"], ensure_ascii=False))
        data = "none"
    parts = [{"text": EXPLAIN_PROMPT % (intro, data)}]
    if image:
        parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(image).decode()}})
    try:
        ex = gemini_json(parts, "explain")
    except Exception as exc:
        if not a:
            raise
        print(f"explain failed: {exc}; sending numbers only")
        ex = {"summary": "Izoh hozir tayyorlanmadi, quyida hisoblangan darajalar."}
    ex = {k: (" ".join(v) if isinstance(v, list) else str(v)) for k, v in ex.items() if v}

    text = format_text(meta, a, ex, image_only=not a)
    if a:
        side = a["plan"]["side"]
        cap_line = f"🧠 <b>Buffet AI · {esc(meta['title'])}</b> · {esc(TF_NAMES.get(meta['tf'], meta['tf']))}\n" \
                   f"Reja: <b>{side}</b> · RSI {a['rsi']} · trend: {a['trend']}" + \
                   {"approved": "\n✅ 5 yillik sinovdan o'tgan qoidaga mos", "not_proven":
                    "\n⚠️ 5 yillik sinovda tasdiqlanmagan signal"}.get((a.get("evidence") or {}).get("status"), "")
        send_photo(make_chart(meta, a), cap_line)
        send_text(text, reply=False, html_mode=True)
    else:
        send_text(text, html_mode=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}")
        try:
            send_text("❌ Grafikni tahlil qilib bo'lmadi. Birozdan keyin qayta urinib ko'ring.")
        finally:
            delete_ack()
            raise SystemExit(1)
    delete_ack()
