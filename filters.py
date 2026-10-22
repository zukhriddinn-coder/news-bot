"""
Trading Laboratory -> TOP 10 filter analysis (Penny / Growth / Value)
A subscriber writes "Value: $TSLA" (or Growth: / Penny:). Google Apps Script starts this
script (GitHub Actions). It checks the 10 filters of that framework:
  - number filters are calculated in code from Yahoo Finance data,
  - news / sentiment filters are judged by Gemini from collected evidence
    (Google News headlines, StockTwits, Reddit, + Google Search when available),
then posts a score card image and a formatted text card.
"""

import html
import io
import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from calendar import timegm
from datetime import date, datetime, timezone

import feedparser
import yfinance as yf

# ---------------------------------------------------------------------------
# SETTINGS (you can edit these)
# ---------------------------------------------------------------------------

GEMINI_MODELS = ["gemini-flash-latest", "gemini-2.5-flash", "gemini-flash-lite-latest",
                 "gemini-2.5-flash-lite"]
USE_GOOGLE_SEARCH = True       # let Gemini search Google too (falls back without it)
VALUE_COUNT_NEUTRAL = False    # True = Value verdict counts 🟡 (ehtiyotkor) together with 🟢
DISCLAIMER = "⚠️ Bu investitsion maslahat emas. Ma'lumotlar: Yahoo Finance, Google News, StockTwits."

SECTOR_ETF = {
    "Technology": "XLK", "Communication Services": "XLC", "Healthcare": "XLV",
    "Financial Services": "XLF", "Consumer Cyclical": "XLY", "Consumer Defensive": "XLP",
    "Energy": "XLE", "Industrials": "XLI", "Basic Materials": "XLB", "Real Estate": "XLRE",
    "Utilities": "XLU",
}
HOT_SECTOR = re.compile(r"technology|software|semiconductor|computer|electronic|internet|biotech|drug|"
                        r"pharma|aerospace|defense|energy|oil|gas|uranium|solar|nuclear|"
                        r"artificial intelligence|\bai\b", re.I)
TOP_FUNDS = re.compile(r"vanguard|blackrock|fidelity|fmr|tiger|state street|t\. ?rowe|capital research|"
                       r"geode|jpmorgan|morgan stanley|invesco", re.I)

# ---------------------------------------------------------------------------

BOT_TOKEN = re.sub(r"\s+", "", os.environ.get("VALUE_BOT_TOKEN", ""))
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
MODE = os.environ.get("MODE", "").strip().lower()
TICKER_IN = os.environ.get("TICKER", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()
THREAD_ID = os.environ.get("THREAD_ID", "").strip()
REPLY_TO = os.environ.get("REPLY_TO", "").strip()
ACK_ID = os.environ.get("ACK_ID", "").strip()
UA = "Mozilla/5.0 (compatible; TradingLabBot/1.0)"

ICON = {"pass": "🟢", "neutral": "🟡", "fail": "🔴", None: "⚪"}

# The three frameworks (from the Trading Laboratory templates)
FRAMEWORKS = {
    "growth": {"title": "GROWTH", "filters": [
        ("F1", "Daromad hajmi", "Yillik savdo hajmi market capning kamida 10% bo'lishi kerak."),
        ("F2", "Institutsional egalik", "Institutsional investorlar ulushi 10% dan ko'p bo'lishi kerak."),
        ("F3", "Offering yo'qligi", "So'nggi 3 oy ichida yangi offering bo'lmasligi kerak. PASS: offering yo'q; FAIL: so'nggi 3 oyda offering e'lon qilingan."),
        ("F4", "Yangiliklar sifati", "So'nggi yangiliklar orasida kamida 5 ta ijobiy xabar. PASS: 5+ ijobiy; NEUTRAL: 2-4 ijobiy (aralash); FAIL: ijobiy yangilik kam yoki yo'q."),
        ("F5", "Narx harakati tarixi", "So'nggi 2 yilda haftada 20-30%+ o'sish kamida 2 marta."),
        ("F6", "Likvidlik", "Yetarli savdo hajmi, tor spread."),
        ("F7", "Doimiy daromad o'sishi", "Yillik yoki choraklik daromad izchil o'sishi kerak."),
        ("F8", "Analyst price target", "Konsensus maqsad joriy narxdan yuqori bo'lishi kerak."),
        ("F9", "CEO tajribasi va faolligi", "CEO tajribali va ijtimoiy tarmoqlarda (X, LinkedIn) faol bo'lishi kerak. PASS: tajribali + faol; NEUTRAL: tajribali lekin kam faol, yoki yangi CEO; FAIL: tajribasiz yoki ko'rinmaydigan rahbariyat."),
        ("F10", "Ijtimoiy sentiment", "X, Discord, Reddit'da ijobiy muhokama. PASS: asosan ijobiy, faol muhokama; NEUTRAL: aralash yoki kam muhokama; FAIL: salbiy yoki muhokama yo'q."),
    ]},
    "penny": {"title": "PENNY", "filters": [
        ("F1", "Float", "Float 50 milliondan kam bo'lishi kerak."),
        ("F2", "Market cap", "Bozor kapitalizatsiyasi $50M dan kam bo'lishi kerak."),
        ("F3", "AQSh kompaniyasi", "Kompaniya AQShda ro'yxatdan o'tgan bo'lishi kerak."),
        ("F4", "Reverse split xavfi", "Kelgusi 2 haftada tasdiqlangan va rejalashtirilgan reverse split bo'lmasligi kerak. Uzoq muddatli yoki faqat ovozga qo'yilgan RS FAIL emas. FAIL faqat kelgusi 2 hafta ichida tasdiqlangan RS bo'lsa."),
        ("F5", "Offering xavfi", "So'nggi 1 oy ichida yangi offering yangiligi bo'lmasligi kerak. 1 oydan oldingi offering PASS (warrantlar aktiv bo'lsa ham). FAIL faqat so'nggi 1 oyda yangi offering e'lon qilingan bo'lsa."),
        ("F6", "Sektor", "Issiq sektor: Tech / Defense / Biotech / AI / Energy."),
        ("F7", "Price spike tarixi", "So'nggi 2 yilda haftada 60%+ spike kamida 2 marta (keyingi tushish muhim emas)."),
        ("F8", "Moliyaviy salomatlik va menejment", "2+ yillik cash runway, insider buying, kuchli (yangi) boshqaruv, ijobiy menejment o'zgarishlari: shulardan 2-3 tasi bo'lsa PASS. FAIL: cash yo'q, insider selling, zaif menejment."),
        ("F9", "Yangiliklar va o'sish signallari", "So'nggi 12 oyda asosan ijobiy yangiliklar + 2026 o'sish rejalari aniq. PASS: asosan ijobiy katalizatorlar + aniq o'sish rejalari; NEUTRAL: aralash; FAIL: asosan salbiy yoki yangilik yo'q."),
        ("F10", "Winner profile moslik", "Benchmark g'oliblar: WORX, OLB, TPET, MOBX, CXAI, ASNS, SAFX. Belgilar: float < 30M, market cap < $20M, qattiq katalizator (hukumat/mudofaa shartnomasi, sektor mavzusi), US-domiciled, dilution yo'q/tozalangan, yuqori short interest, sektor narrativi (mudofaa, AI, fintech, health), run oldidan social buzz, hajm o'rtachadan 5-20x yuqori. Taniqli underperformerlar: IXHL, TDIC, ITRM, MEHA, QH, LIMN. PASS: ko'pchilik belgilar mos; FAIL: mos emas."),
    ]},
    "value": {"title": "VALUE", "filters": [
        ("F1", "Daromad o'sishi", "So'nggi 2 chorak daromadi o'syaptimi? Yillik 10%+ o'sish 🟢, 0-10% 🟡, pasayish 🔴."),
        ("F2", "Gross margin barqarorligi", "Gross margin 50%+ va barqaror yoki kengaymoqda 🟢; siqilmoqda 🔴."),
        ("F3", "Institutsional qo'llab-quvvatlash", "Top fondlar (Fidelity, Vanguard, BlackRock, Tiger) so'nggi chorakda ulushini oshirganmi? Oshirgan 🟢, qisqartirgan 🔴."),
        ("F4", "Earnings surprise tarixi", "So'nggi 4 chorakda EPS kutilgandan yuqori: 3+ marta 🟢, 2 marta 🟡, 1 yoki 0 🔴."),
        ("F5", "Earnings sanasiga masofa", "Keyingi hisobot 3 haftadan uzoq 🟢, 1-3 hafta 🟡, 1 haftadan yaqin 🔴."),
        ("F6", "Analitik konsensus", "Konsensus Buy/Strong Buy va maqsad joriy narxdan 10%+ yuqori 🟢, Hold 🟡, Sell 🔴."),
        ("F7", "Relative Strength", "So'nggi 12 oyda S&P 500 ga nisbatan kuch: RS 70+ 🟢, 50-70 🟡, 50 dan past 🔴."),
        ("F8", "Balans sog'lomligi", "Naqd pul > qarz yoki D/E past, operatsion pul oqimi musbat 🟢; aralash 🟡; qarz yuqori 🔴."),
        ("F9", "So'nggi katalizator", "So'nggi 30 kunda muhim ijobiy yangilik (shartnoma, mahsulot, buyback, hamkorlik) 🟢, neytral 🟡, salbiy 🔴."),
        ("F10", "Sektor makro muhiti", "Sektor ETF (XLK, XLC, XLV...) 50 kunlik MA dan yuqori 🟢, chegarada 🟡, pastda 🔴."),
    ]},
}

# filters judged by Gemini (the rest are calculated)
AI_FILTERS = {"growth": {"F3", "F4", "F9", "F10"},
              "penny": {"F4", "F5", "F8", "F9", "F10"},
              "value": {"F9"}}


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
    p = {**where(reply), "text": text, "link_preview_options": {"is_disabled": True}}
    if html_mode:
        p["parse_mode"] = "HTML"
    tg("sendMessage", p)


def send_photo(png, caption):
    tg("sendPhoto", {**where(), "caption": caption[:1024], "parse_mode": "HTML"},
       files={"photo": ("filters.png", png, "image/png")})


def delete_ack():
    if ACK_ID:
        try:
            tg("deleteMessage", {"chat_id": CHAT_ID, "message_id": int(ACK_ID)})
        except Exception as exc:
            print(f"Could not delete the waiting message: {exc}")


# ----------------------------- Helpers --------------------------------------

def num(v):
    try:
        v = float(v)
        return None if math.isnan(v) or math.isinf(v) else v
    except (TypeError, ValueError):
        return None


def big(v):
    if v is None:
        return "—"
    a = abs(v)
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            return f"{v / div:,.1f}{suf}"
    return f"{v:,.0f}"


def pct(v, sign=True):
    return "—" if v is None else (f"{v * 100:+.1f}%" if sign else f"{v * 100:.1f}%")


def safe(label, fn, default=None):
    try:
        return fn()
    except Exception as exc:
        print(f"Yahoo {label}: {exc}")
        return default


def row_values(df, name, n=8):
    """Values of one statement row, newest first."""
    if df is None or getattr(df, "empty", True) or name not in df.index:
        return []
    out = []
    for col in list(df.columns)[:n]:
        v = num(df.loc[name, col])
        if v is not None:
            out.append(v)
    return out


def R(status, value, note=""):
    return {"status": status, "value": value, "note": note}


# ----------------------------- Data -----------------------------------------

def collect(symbol):
    t = yf.Ticker(symbol)
    info = safe("info", lambda: t.info, {}) or {}
    name = info.get("longName") or info.get("shortName")
    price = num(info.get("currentPrice")) or num(info.get("regularMarketPrice"))
    if not name or not price:
        return None
    d = {"t": t, "symbol": symbol, "name": name, "price": price, "info": info}
    d["weekly"] = safe("weekly", lambda: t.history(period="2y", interval="1wk", auto_adjust=False))
    d["q_income"] = safe("quarterly income", lambda: t.quarterly_income_stmt)
    d["a_income"] = safe("annual income", lambda: t.income_stmt)
    return d


def weekly_moves(weekly, threshold, use_high):
    """How many weeks in the data rose at least `threshold` (0.2 = 20%) from last week's close."""
    if weekly is None or weekly.empty or "Close" not in weekly:
        return None, []
    prev = weekly["Close"].shift(1)
    move = (weekly["High"] if use_high else weekly["Close"]) / prev - 1
    hits = move[move >= threshold].dropna()
    return len(hits), [(idx.strftime("%d.%m.%Y"), float(v)) for idx, v in hits.items()][-6:]


# ----------------------------- Calculated filters ---------------------------

def growth_filters(d):
    i, p = d["info"], d["price"]
    out = {}

    rev, mc = num(i.get("totalRevenue")), num(i.get("marketCap"))
    if rev and mc:
        r = rev / mc
        out["F1"] = R("pass" if r >= 0.10 else "fail", f"Savdo ${big(rev)} = market capning {r * 100:.1f}%")

    inst = num(i.get("heldPercentInstitutions"))
    if inst is not None:
        out["F2"] = R("pass" if inst > 0.10 else "fail", f"Institutsional ulush {inst * 100:.1f}%")

    n, hits = weekly_moves(d["weekly"], 0.20, use_high=False)
    if n is not None:
        out["F5"] = R("pass" if n >= 2 else "neutral" if n == 1 else "fail",
                      f"2 yilda {n} marta haftalik +20%",
                      ", ".join(f"{a} ({b * 100:+.0f}%)" for a, b in hits[-3:]))

    vol = num(i.get("averageVolume"))
    if vol:
        dollar = vol * p
        bid, ask = num(i.get("bid")), num(i.get("ask"))
        spread = (ask - bid) / ((ask + bid) / 2) if bid and ask and ask >= bid > 0 else None
        tight = spread is None or spread <= 0.005
        status = ("pass" if dollar >= 20e6 and vol >= 1e6 and tight
                  else "neutral" if dollar >= 2e6 else "fail")
        sp = f", spread {spread * 100:.2f}%" if spread is not None else ""
        out["F6"] = R(status, f"O'rtacha hajm {big(vol)} (${big(dollar)}/kun){sp}")

    annual = row_values(d["a_income"], "Total Revenue", 4)
    q_yoy = num(i.get("revenueGrowth"))
    if len(annual) >= 2 or q_yoy is not None:
        growths = [annual[k] / annual[k + 1] - 1 for k in range(min(2, len(annual) - 1)) if annual[k + 1]]
        signals = growths + ([q_yoy] if q_yoy is not None else [])
        ups = sum(1 for g in signals if g > 0)
        status = "pass" if ups == len(signals) else "fail" if ups == 0 else "neutral"
        parts = [f"yillik {g * 100:+.0f}%" for g in growths] + ([f"chorak YoY {q_yoy * 100:+.0f}%"]
                                                              if q_yoy is not None else [])
        out["F7"] = R(status, ", ".join(parts))

    mean = num(i.get("targetMeanPrice"))
    if mean:
        diff = mean / p - 1
        out["F8"] = R("pass" if diff > 0.05 else "neutral" if diff >= -0.05 else "fail",
                      f"Maqsad ${mean:,.2f} ({diff * 100:+.1f}%)")
    else:
        out["F8"] = R("fail", "Analitiklar maqsadi yo'q")
    return out


def penny_filters(d):
    i = d["info"]
    out = {}
    flt = num(i.get("floatShares"))
    if flt:
        out["F1"] = R("pass" if flt < 50e6 else "fail", f"Float {big(flt)}")
    mc = num(i.get("marketCap"))
    if mc:
        out["F2"] = R("pass" if mc < 50e6 else "fail", f"Market cap ${big(mc)}")
    country = i.get("country")
    if country:
        out["F3"] = R("pass" if country == "United States" else "fail", f"Mamlakat: {country}")
    sector = f"{i.get('sector', '')} / {i.get('industry', '')}".strip(" /")
    if sector:
        out["F6"] = R("pass" if HOT_SECTOR.search(sector) else "fail", sector)
    n, hits = weekly_moves(d["weekly"], 0.60, use_high=True)
    if n is not None:
        out["F7"] = R("pass" if n >= 2 else "fail", f"2 yilda {n} marta haftalik +60% spike",
                      ", ".join(f"{a} ({b * 100:+.0f}%)" for a, b in hits[-3:]))
    return out


def value_filters(d):
    i, p, t = d["info"], d["price"], d["t"]
    out = {}

    yoy = num(i.get("revenueGrowth"))
    q = row_values(d["q_income"], "Total Revenue", 3)
    if yoy is not None:
        qoq = f", oxirgi chorak QoQ {(q[0] / q[1] - 1) * 100:+.1f}%" if len(q) >= 2 and q[1] else ""
        out["F1"] = R("pass" if yoy >= 0.10 else "neutral" if yoy >= 0 else "fail",
                      f"Chorak daromadi YoY {yoy * 100:+.1f}%{qoq}")

    gp = row_values(d["q_income"], "Gross Profit", 4)
    rv = row_values(d["q_income"], "Total Revenue", 4)
    margins = [g / r for g, r in zip(gp, rv) if r]
    if margins:
        last = margins[0]
        trend = last - sum(margins[1:]) / len(margins[1:]) if len(margins) > 1 else 0
        status = ("fail" if trend < -0.02 else "pass" if last >= 0.50 and trend >= -0.01 else "neutral")
        word = "kengaymoqda" if trend > 0.01 else "siqilmoqda" if trend < -0.01 else "barqaror"
        out["F2"] = R(status, f"Gross margin {last * 100:.1f}% ({word}, {trend * 100:+.1f} p.p.)")
    else:
        out["F2"] = R("neutral", "Gross margin ko'rsatilmaydi (sohaga xos)")

    holders = safe("institutional holders", lambda: t.institutional_holders)
    if holders is not None and not holders.empty and "pctChange" in holders.columns:
        top = holders[holders["Holder"].astype(str).str.contains(TOP_FUNDS)]
        top = top if not top.empty else holders
        changes = [num(v) for v in top["pctChange"]]
        up = sum(1 for c in changes if c and c > 0)
        down = sum(1 for c in changes if c and c < 0)
        out["F3"] = R("pass" if up > down else "fail" if down > up else "neutral",
                      f"Top fondlar: {up} ta oshirgan, {down} ta kamaytirgan")

    eh = safe("earnings history", lambda: t.earnings_history)
    if eh is not None and not eh.empty and {"epsActual", "epsEstimate"} <= set(eh.columns):
        last4 = eh.sort_index().tail(4)
        beats = sum(1 for a, e in zip(last4["epsActual"], last4["epsEstimate"])
                    if num(a) is not None and num(e) is not None and a > e)
        out["F4"] = R("pass" if beats >= 3 else "neutral" if beats == 2 else "fail",
                      f"So'nggi {len(last4)} chorakda {beats} marta kutilmadan yuqori")

    next_date = None
    cal = safe("calendar", lambda: t.calendar, {}) or {}
    dates = [x for x in (cal.get("Earnings Date") or []) if isinstance(x, date)]
    if dates:
        next_date = min(dates)
    elif num(i.get("earningsTimestampStart")) or num(i.get("earningsTimestamp")):
        ts = num(i.get("earningsTimestampStart")) or num(i.get("earningsTimestamp"))
        next_date = datetime.fromtimestamp(ts, timezone.utc).date()
    if next_date and next_date >= date.today():
        days = (next_date - date.today()).days
        out["F5"] = R("pass" if days > 21 else "neutral" if days >= 7 else "fail",
                      f"Keyingi hisobot {next_date:%d.%m.%Y} ({days} kun)")

    rec, mean = i.get("recommendationKey"), num(i.get("targetMeanPrice"))
    if rec or mean:
        diff = mean / p - 1 if mean else None
        if rec in ("buy", "strong_buy") and diff is not None and diff >= 0.10:
            status = "pass"
        elif rec in ("sell", "strong_sell", "underperform"):
            status = "fail"
        else:
            status = "neutral"
        label = {"strong_buy": "Strong Buy", "buy": "Buy", "hold": "Hold", "underperform": "Underperform",
                 "sell": "Sell", "strong_sell": "Strong Sell"}.get(rec, "—")
        out["F6"] = R(status, f"{label}, maqsad {('$' + format(mean, ',.2f')) if mean else '—'} ({pct(diff)})")

    def rs():
        s = t.history(period="1y")["Close"]
        m = yf.Ticker("SPY").history(period="1y")["Close"]
        return float(s.iloc[-1] / s.iloc[0] - 1), float(m.iloc[-1] / m.iloc[0] - 1)
    r = safe("relative strength", rs)
    if r:
        gap = r[0] - r[1]
        out["F7"] = R("pass" if gap >= 0.10 else "neutral" if gap >= -0.10 else "fail",
                      f"12 oy: aksiya {r[0] * 100:+.0f}%, S&P 500 {r[1] * 100:+.0f}%",
                      "Taxminiy RS: S&P 500 dan 10 p.p. yuqori ≈ 70+, 10 p.p. gacha past ≈ 50–70.")

    cash, debt = num(i.get("totalCash")), num(i.get("totalDebt"))
    ocf, de = num(i.get("operatingCashflow")), num(i.get("debtToEquity"))
    if cash is not None or ocf is not None:
        cash, debt = cash or 0, debt or 0
        if ocf and ocf > 0 and (cash > debt or (de is not None and de < 100)):
            status = "pass"
        elif (ocf is not None and ocf <= 0 and debt > cash) or (de is not None and de > 200):
            status = "fail"
        else:
            status = "neutral"
        de_txt = f", D/E {de / 100:.2f}" if de is not None else ""
        out["F8"] = R(status, f"Naqd ${big(cash)}, qarz ${big(debt)}, OCF ${big(ocf)}{de_txt}")

    etf = SECTOR_ETF.get(i.get("sector"))
    if etf:
        def ma():
            c = yf.Ticker(etf).history(period="6mo")["Close"]
            return float(c.iloc[-1]), float(c.tail(50).mean())
        m = safe("sector ETF", ma)
        if m:
            gap = m[0] / m[1] - 1
            out["F10"] = R("pass" if gap > 0.01 else "neutral" if gap >= -0.01 else "fail",
                           f"{etf} 50 kunlik MA dan {gap * 100:+.1f}%")
    return out


# ----------------------------- Evidence for Gemini --------------------------

def google_news(query, limit=15):
    url = "https://news.google.com/rss/search?hl=en-US&gl=US&ceid=US:en&q=" + urllib.parse.quote(query)
    feed = feedparser.parse(url, agent=UA)
    out = []
    for e in feed.entries[:limit]:
        t = e.get("published_parsed")
        src = (e.get("source") or {}).get("title", "")
        title = e.get("title", "")
        if src and title.endswith(" - " + src):
            title = title[: -len(src) - 3]
        out.append(f"{datetime.fromtimestamp(timegm(t), timezone.utc):%Y-%m-%d} | {src} | {title}" if t
                   else f"? | {src} | {title}")
    return out


def stocktwits(symbol):
    req = urllib.request.Request(f"https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json",
                                 headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.load(resp)
    msgs = data.get("messages", [])
    bull = sum(1 for m in msgs if ((m.get("entities") or {}).get("sentiment") or {}).get("basic") == "Bullish")
    bear = sum(1 for m in msgs if ((m.get("entities") or {}).get("sentiment") or {}).get("basic") == "Bearish")
    watchers = (data.get("symbol") or {}).get("watchlist_count")
    sample = [re.sub(r"\s+", " ", m.get("body", ""))[:140] for m in msgs[:8]]
    return {"messages": len(msgs), "bullish": bull, "bearish": bear, "watchers": watchers, "sample": sample}


def reddit(symbol):
    url = f"https://www.reddit.com/search.json?q=%24{symbol}&sort=new&t=month&limit=20"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.load(resp)
    return [f"r/{p['data'].get('subreddit')} | {p['data'].get('score')} up | {p['data'].get('title', '')[:120]}"
            for p in data.get("data", {}).get("children", [])][:12]


def evidence(d, mode, ai_ids):
    s, name, i = d["symbol"], re.sub(r",?\s+(Inc\.?|Corp\.?|Corporation|Ltd\.?|plc)$", "", d["name"]), d["info"]
    ev = {}

    def add(key, fn):
        try:
            value = fn()
            if value:
                ev[key] = value
        except Exception as exc:
            print(f"Evidence {key}: {exc}")

    add("news_30_days", lambda: google_news(f'("{name}" OR "{s}") stock when:30d', 20))
    if "F3" in ai_ids or "F5" in ai_ids:
        days = 90 if mode == "growth" else 45
        add("offering_news", lambda: google_news(
            f'("{name}" OR "{s}") (offering OR "registered direct" OR "private placement" OR "at-the-market" '
            f'OR dilution OR warrants) when:{days}d', 12))
    if mode == "penny":
        add("reverse_split_news", lambda: google_news(f'("{name}" OR "{s}") "reverse split" when:90d', 10))
        add("news_12_months", lambda: google_news(f'("{name}" OR "{s}") when:365d', 20))
    if "F9" in ai_ids and mode == "growth":
        add("ceo_news", lambda: google_news(f'"{name}" CEO when:365d', 10))
    if mode in ("growth", "penny"):
        add("stocktwits", lambda: stocktwits(s))
        add("reddit_last_month", lambda: reddit(s))

    officers = [{"name": o.get("name"), "title": o.get("title"), "age": o.get("age")}
                for o in (i.get("companyOfficers") or [])[:4]]
    if officers:
        ev["officers"] = officers
    if mode == "penny":
        ocf, cash = num(i.get("operatingCashflow")), num(i.get("totalCash"))
        runway = (f"{cash / -ocf:.1f} yil" if cash and ocf and ocf < 0
                  else "pul oqimi musbat (runway muammo emas)" if ocf and ocf > 0 else "noma'lum")
        ev["cash_runway"] = {"cash": cash, "operating_cash_flow_year": ocf, "runway": runway}
        ins = safe("insider purchases", lambda: d["t"].insider_purchases)
        if ins is not None and not ins.empty:
            ev["insider_6m"] = {str(r[ins.columns[0]]): {"shares": num(r.get("Shares")), "trans": num(r.get("Trans"))}
                                for _, r in ins.iterrows()}
        avg, vol = num(i.get("averageVolume")), num(i.get("regularMarketVolume") or i.get("volume"))
        ev["winner_traits"] = {
            "float": num(i.get("floatShares")), "market_cap": num(i.get("marketCap")),
            "country": i.get("country"), "sector": i.get("sector"), "industry": i.get("industry"),
            "short_percent_of_float": num(i.get("shortPercentOfFloat")),
            "today_volume_vs_avg": round(vol / avg, 1) if vol and avg else None,
        }
    return ev


# ----------------------------- Gemini ---------------------------------------

def gemini_judge(d, mode, ai_ids, calculated, ev):
    fw = FRAMEWORKS[mode]
    todo = [f for f in fw["filters"] if f[0] in ai_ids]
    done = [{"id": k, "status": v["status"], "fakt": v["value"]} for k, v in calculated.items()]
    prompt = f"""Sen professional moliyaviy tahlilchisan (KVANTUS AI uslubi). Bugun {date.today():%Y-%m-%d}.
{fw['title']} STOCK 10-FILTER FRAMEWORK bo'yicha ${d['symbol']} ({d['name']}) aksiyasini tahlil qilyapmiz.
Joriy narx: ${d['price']:,.2f}.

BAHOLANADIGAN FILTRLAR:
{chr(10).join(f"{f[0]} — {f[1]}: {f[2]}" for f in todo)}

DALILLAR (yangiliklar sarlavhalari "sana | manba | sarlavha" ko'rinishida, StockTwits, Reddit, Yahoo):
{json.dumps(ev, ensure_ascii=False, indent=1, default=str)}

KOD HISOBLAGAN FILTRLAR (o'zgartirma, faqat umumiy xulosa uchun):
{json.dumps(done, ensure_ascii=False)}

QOIDALAR:
- Faqat dalillar va (mavjud bo'lsa) Google qidiruvi natijalariga asoslan, o'ylab topma.
- Dalil yetarli bo'lmasa "neutral" qo'y va izohda "ma'lumot yetarli emas" deb yoz.
- "value" — eng muhim fakt, 60 belgigacha. "note" — 1 gaplik izoh, 160 belgigacha.
- "summary" — barcha 10 filtr bo'yicha 2-3 gaplik professional xulosa, aniq faktlar bilan.
- Hammasi o'zbek tilida (lotin yozuvi), markdown belgilarisiz.
Javob sifatida FAQAT quyidagi formatdagi bitta qatorni ber:
RESULTS_JSON: {{"filters": [{{"id": "F3", "status": "pass|neutral|fail", "value": "...", "note": "..."}}], "summary": "..."}}
"""
    base = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2}}
    variants = ([{**base, "tools": [{"google_search": {}}]}] if USE_GOOGLE_SEARCH else []) + [base]
    for payload in variants:
        searched = "tools" in payload
        body = json.dumps(payload).encode("utf-8")
        for model in GEMINI_MODELS:
            for attempt in range(2):
                req = urllib.request.Request(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    data=body, headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY})
                try:
                    with urllib.request.urlopen(req, timeout=240) as resp:
                        res = json.load(resp)
                    parts = res["candidates"][0]["content"]["parts"]
                    text = "\n".join(p.get("text", "") for p in parts)
                    parsed = parse_json(text)
                    if parsed:
                        print(f"Gemini {model} (search: {searched}) ok")
                        return parsed, searched
                    print(f"Gemini {model}: no JSON in answer")
                    break
                except urllib.error.HTTPError as e:
                    msg = e.read().decode("utf-8", "replace")[:300]
                    print(f"Gemini {model} (search: {searched}): HTTP {e.code} {msg}")
                    if e.code in (401, 403) and "API key" in msg:
                        return None, False
                    if e.code in (400, 403) and searched:
                        break                          # search tool not allowed: try next model / no search
                    if e.code == 404 or (e.code == 429 and "PerDay" in msg):
                        break
                    time.sleep(15 * (attempt + 1))
                except Exception as exc:
                    print(f"Gemini {model}: {exc}")
                    time.sleep(5)
    return None, False


def parse_json(text):
    m = re.search(r"RESULTS_JSON:\s*(?:```(?:json)?\s*)?(\{.*\})", text, re.S) \
        or re.search(r"(\{\s*\"filters\".*\})", text, re.S)
    if not m:
        return None
    raw = m.group(1)
    for end in range(len(raw), 0, -1):
        if raw[end - 1] != "}":
            continue
        for chunk in (raw[:end], re.sub(r",\s*([}\]])", r"\1", raw[:end])):
            try:
                return json.loads(chunk)
            except json.JSONDecodeError:
                pass
    return None


# ----------------------------- Verdict --------------------------------------

def verdict(mode, results):
    green = sum(1 for r in results.values() if r["status"] == "pass")
    yellow = sum(1 for r in results.values() if r["status"] == "neutral")
    if mode == "growth":
        score = green
        v = ("🟢 STRONG BUY" if score >= 9 else "🟢 BUY" if score >= 7 else "🟡 WATCH" if score >= 5
             else "🔴 HARD PASS")
        rule = "🟢 soni: 9–10 Strong Buy · 7–8 Buy · 5–6 Watch · 0–4 Hard Pass"
    elif mode == "penny":
        score = green
        v = ("🟢 STRONG BUY" if score >= 8 else "🟢 BUY" if score >= 6 else "🟡 WATCH" if score >= 4
             else "🔴 HARD PASS")
        rule = "PASS soni: 8–10 Strong Buy · 6–7 Buy · 4–5 Watch · 0–3 Hard Pass"
    else:
        score = green + (yellow if VALUE_COUNT_NEUTRAL else 0)
        v = "🟢 STRONG BUY" if score >= 7 else "🟡 BUY (ehtiyot bilan)" if score >= 5 else "🔴 DO NOT BUY"
        rule = ("🟢+🟡 soni: 7+ Strong Buy · 5–6 Buy (ehtiyot bilan) · 5 dan kam Do Not Buy"
                if VALUE_COUNT_NEUTRAL else "🟢 soni: 7+ Strong Buy · 5–6 Buy · 5 dan kam Do Not Buy")
    return score, v, rule, green, yellow


# ----------------------------- Output ---------------------------------------

THEME = {"bg": "#0b1220", "panel": "#111a2e", "line": "#1e2a44", "ink": "#e6edf7", "muted": "#8a99b8",
         "pass": "#22c55e", "neutral": "#eab308", "fail": "#ef4444", None: "#64748b"}


def _font():
    from matplotlib import font_manager
    names = {f.name for f in font_manager.fontManager.ttflist}
    return "Inter" if "Inter" in names else "DejaVu Sans"


def make_card(d, mode, results, score, v):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    T = THEME
    plt.rcParams["font.family"] = _font()
    fw = FRAMEWORKS[mode]
    rows = fw["filters"]
    fig_h = 2.95 + 0.56 * len(rows)
    fig = plt.figure(figsize=(10, fig_h), dpi=160, facecolor=T["bg"])
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_axis_off()
    ax.set_xlim(0, 1)
    ax.set_ylim(0, fig_h)
    y = fig_h

    ax.text(0.04, y - 0.55, f"${d['symbol']}", fontsize=28, fontweight="bold", color=T["ink"], va="center")
    ax.text(0.04, y - 1.05, d["name"][:44], fontsize=12.5, color=T["ink"], va="center")
    ax.text(0.04, y - 1.42, f"{fw['title']} STOCK · 10-FILTER · {datetime.now(timezone.utc):%d.%m.%Y}",
            fontsize=9.5, color=T["muted"], va="center")
    ax.text(0.60, y - 0.42, "Joriy narx", fontsize=9.5, color=T["muted"], va="center")
    ax.text(0.60, y - 0.80, f"${d['price']:,.2f}", fontsize=20, fontweight="bold", color=T["ink"], va="center")
    ax.text(0.79, y - 0.42, "Ball", fontsize=9.5, color=T["muted"], va="center")
    ax.text(0.79, y - 0.80, f"{score}/10", fontsize=20, fontweight="bold", color=T["ink"], va="center")
    tone = T["pass"] if "🟢" in v else T["neutral"] if "🟡" in v else T["fail"]
    ax.text(0.60, y - 1.35, " " + v.split(" ", 1)[1] + " ", fontsize=11, fontweight="bold", color=tone,
            va="center", bbox=dict(boxstyle="round,pad=0.45,rounding_size=0.8", facecolor=T["bg"],
                                   edgecolor=tone, linewidth=1.3))
    ax.plot([0.04, 0.96], [y - 1.85, y - 1.85], color=T["line"], linewidth=1)

    y0 = y - 2.15
    for k, (fid, name, _) in enumerate(rows):
        r = results.get(fid, {"status": None, "value": "Aniqlab bo'lmadi"})
        cy = y0 - k * 0.56
        if k % 2 == 0:
            ax.add_patch(plt.Rectangle((0.03, cy - 0.28), 0.94, 0.56, color=T["panel"], linewidth=0))
        ax.scatter([0.065], [cy], s=150, color=T[r["status"]], zorder=3, linewidth=0)
        ax.text(0.095, cy, f"{fid}", fontsize=9, color=T["muted"], va="center")
        ax.text(0.14, cy, name, fontsize=11, color=T["ink"], va="center")
        value = str(r.get("value") or "")
        ax.text(0.955, cy, value[:52] + ("…" if len(value) > 52 else ""), fontsize=9.5,
                color=T["muted"], va="center", ha="right")

    x = 0.045
    for key, label in (("pass", "o'tdi"), ("neutral", "ehtiyotkor / aralash"), ("fail", "o'tmadi"),
                       (None, "ma'lumot yo'q")):
        ax.scatter([x], [0.35], s=40, color=T[key], linewidth=0)
        ax.text(x + 0.012, 0.35, label, fontsize=8, color=T["muted"], va="center")
        x += 0.04 + 0.0062 * len(label)
    ax.text(0.96, 0.35, "Trading Laboratory · Investitsion maslahat emas", fontsize=8, color=T["muted"],
            va="center", ha="right")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=T["bg"])
    plt.close(fig)
    return buf.getvalue()


def esc(s):
    return html.escape(str(s or "").replace("**", "").strip(), quote=False)


def format_text(d, mode, results, score, v, rule, summary, searched):
    fw = FRAMEWORKS[mode]
    lines = [f"🔍 <b>{fw['title']} · ${esc(d['symbol'])} — {esc(d['name'])}</b>",
             f"<i>10 filtr bo'yicha tahlil · joriy narx ${d['price']:,.2f}</i>", ""]
    for fid, name, _ in fw["filters"]:
        r = results.get(fid, {"status": None, "value": "Aniqlab bo'lmadi"})
        lines.append(f"{ICON[r['status']]} <b>{fid} · {esc(name)}</b>")
        if r.get("value"):
            lines.append(f"    {esc(r['value'])}")
        if r.get("note"):
            lines.append(f"    <i>{esc(r['note'])}</i>")
        lines.append("")
    lines += ["━━━━━━━━━━━━━━━",
              f"🏁 <b>Natija: {score}/10 → {v}</b>",
              f"<i>{esc(rule)}</i>"]
    if summary:
        lines.append(f"<blockquote>{esc(summary)}</blockquote>")
    if not searched:
        lines.append("<i>ℹ️ Yangilik va sentiment filtrlari Google News, StockTwits va Reddit dalillari "
                     "asosida baholandi.</i>")
    lines += ["", f"<i>{esc(DISCLAIMER)}</i>"]
    return "\n".join(lines).strip()


# ----------------------------- Main -----------------------------------------

def main():
    mode = MODE if MODE in FRAMEWORKS else None
    symbol = TICKER_IN.upper().lstrip("$").replace(".", "-")
    if not mode:
        send_text("❌ Tahlil turi noto'g'ri. Masalan: Value: $TSLA, Growth: $NVDA, Penny: $SNDL")
        return
    if not re.fullmatch(r"[A-Z][A-Z\-]{0,7}", symbol):
        send_text(f"❌ \"{TICKER_IN}\" — noto'g'ri tiker. Masalan: {mode.title()}: $TSLA")
        return
    d = collect(symbol)
    if not d:
        send_text(f"❌ ${symbol} topilmadi. Tikerni tekshiring.")
        return
    print(f"{mode} {symbol}: {d['name']}, ${d['price']}")

    calculated = {"growth": growth_filters, "penny": penny_filters, "value": value_filters}[mode](d)
    all_ids = [f[0] for f in FRAMEWORKS[mode]["filters"]]
    ai_ids = AI_FILTERS[mode] | {f for f in all_ids if f not in calculated}   # missing data -> Gemini
    print(f"Calculated: {sorted(calculated)}, Gemini: {sorted(ai_ids)}")

    ev = evidence(d, mode, ai_ids)
    judged, searched = gemini_judge(d, mode, ai_ids, calculated, ev) if GEMINI_API_KEY else (None, False)
    results = dict(calculated)
    summary = ""
    if judged:
        for f in judged.get("filters") or []:
            fid = str(f.get("id", "")).upper()
            if fid in ai_ids and f.get("status") in ("pass", "neutral", "fail"):
                results[fid] = R(f["status"], str(f.get("value", ""))[:80], str(f.get("note", ""))[:220])
        summary = str(judged.get("summary") or "")[:700]
    for fid in all_ids:
        results.setdefault(fid, R(None, "Aniqlab bo'lmadi", "Ma'lumot topilmadi."))

    score, v, rule, _, _ = verdict(mode, results)
    caption = (f"🔍 <b>{FRAMEWORKS[mode]['title']} · ${esc(symbol)} — {esc(d['name'])}</b>\n"
               f"Natija: <b>{score}/10 → {v}</b>")
    send_photo(make_card(d, mode, results, score, v), caption)
    send_text(format_text(d, mode, results, score, v, rule, summary, searched), reply=False, html_mode=True)
    print(f"Done: {score}/10 {v}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}")
        try:
            send_text(f"❌ {MODE.title()}: ${TICKER_IN.upper().lstrip('$')} bo'yicha tahlil qilib bo'lmadi. "
                      "Birozdan keyin qayta urinib ko'ring.")
        finally:
            delete_ack()
            raise SystemExit(1)
    delete_ack()
