"""
Trading Laboratory -> "Hisobotlar va makro kalendar" bot
Every US trading day Google Apps Script starts this script twice on GitHub Actions; each run posts one card + text
to the "Hisobotlar" topic:
  - 03:00 New York (MODE=pre), 1 hour before the pre-market: companies reporting before the open, the macro
    events of the day, yesterday's results (Monday: also the week ahead)
  - 15:00 New York (MODE=post), 1 hour before the after-market: companies reporting after the close and the
    results that came out this morning
Details:
  - for each company: expected EPS, expected revenue (analyst consensus), approximate net income (EPS x shares),
    and the colour of the year-on-year expectation: 🟢 higher than a year ago, 🔴 lower, 🟡 about the same
  - if a company has already reported: actual EPS vs expected (beat / miss)
  - the macro events of the day (Fed, CPI, jobs, GDP ...) with New York and Tashkent time
  - yesterday's results (EPS beat / miss) of the biggest companies
  - on Monday: a short overview of the whole week
Free data: Finnhub (earnings calendar: expected EPS and revenue, before/after the market) with Yahoo Finance
(company names, market cap, macro calendar, backup when Finnhub is down). A calendar, not a buy or sell signal.
"""

import html
import io
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

import scanner as S                     # New York clock
import buffetai as B                    # card theme and font

# ---------------------------------------------------------------------------
# SETTINGS
# ---------------------------------------------------------------------------

RUN_PRE_NY = (3, 0)                     # 03:00 New York: 1 hour before the pre-market (04:00)
RUN_POST_NY = (15, 0)                   # 15:00 New York: 1 hour before the after-market (16:00; 12:00 on short days)
NYSE_EARLY_CLOSE = {"2026-11-27", "2026-12-24", "2027-11-26"}      # the market closes at 13:00
RUN_WINDOW_MIN = 25                     # Apps Script may be a little late
MIN_CAP = 2_000_000_000                 # only companies above $2 billion market cap
WEEK_MIN_CAP = 10_000_000_000           # Monday overview: only $10 billion and above
MAX_PER_SIDE_TEXT = 14                  # companies listed per session in the text
MAX_PER_SIDE_CARD = 6                   # companies drawn per session on the card
MAX_DETAIL = 36                         # companies we fetch estimates for (largest first)
MAX_MACRO = 8
NYSE_HOLIDAYS = {"2026-11-26", "2026-12-25", "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26",
                 "2027-05-31", "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24"}

# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("EARN_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("EARN_CHAT_ID", "").strip() or "-1003382397608"
THREAD_ID = os.environ.get("EARN_THREAD_ID", "").strip()
FINNHUB_KEY = os.environ.get("FINNHUB_KEY", "").strip()
FORCE = os.environ.get("FORCE", "").strip().lower() in ("1", "true", "yes")
MODE = os.environ.get("MODE", "").strip().lower()          # pre | post (empty: by the clock)
DAY_OVERRIDE = os.environ.get("DAY", "").strip()          # YYYY-MM-DD: preview another day (test)
OYLAR = ["yanvar", "fevral", "mart", "aprel", "may", "iyun", "iyul", "avgust", "sentyabr", "oktyabr", "noyabr",
         "dekabr"]
KUNLAR = ["dushanba", "seshanba", "chorshanba", "payshanba", "juma", "shanba", "yakshanba"]
GREEN, RED, YELLOW = "🟢", "🔴", "🟡"
NY_TZ = "America/New_York"
UZ_TZ = "Asia/Tashkent"
DISCLAIMER = "Investitsion maslahat emas."
SOURCE = {"finnhub": False}                 # set when Finnhub data was really used (shown in the footer)


# ----------------------------- Telegram -------------------------------------

def tg(method, payload, files=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    if files:
        boundary = uuid.uuid4().hex
        body = io.BytesIO()
        for k, v in payload.items():
            body.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n".encode())
            body.write(str(v).encode("utf-8") + b"\r\n")
        for k, (name, data, ctype) in files.items():
            body.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{name}\"\r\n"
                       f"Content-Type: {ctype}\r\n\r\n".encode())
            body.write(data + b"\r\n")
        body.write(f"--{boundary}--\r\n".encode())
        data, ctype = body.getvalue(), f"multipart/form-data; boundary={boundary}"
    else:
        data, ctype = json.dumps(payload).encode("utf-8"), "application/json"
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


def where():
    p = {"chat_id": CHAT_ID}
    if THREAD_ID:
        p["message_thread_id"] = int(THREAD_ID)
    return p


def split_text(text, limit=3900):
    """Telegram allows 4096 characters: cut at blank lines."""
    parts, cur = [], ""
    for block in text.split("\n\n"):
        if cur and len(cur) + len(block) + 2 > limit:
            parts.append(cur)
            cur = block
        else:
            cur = f"{cur}\n\n{block}" if cur else block
    if cur:
        parts.append(cur)
    return parts


def send(png, caption, text):
    res = []
    if png:
        res.append(tg("sendPhoto", {**where(), "caption": caption[:1024], "parse_mode": "HTML"},
                      files={"photo": ("hisobotlar.png", png, "image/png")}))
    for part in split_text(text):
        res.append(tg("sendMessage", {**where(), "text": part, "parse_mode": "HTML",
                                      "link_preview_options": {"is_disabled": True}}))
    for r in res:                                 # where Telegram really put it (to check the topic)
        m = (r or {}).get("result") or {}
        ch = m.get("chat") or {}
        print(f"Telegram: chat '{ch.get('title') or ch.get('first_name') or ''}' ({ch.get('id')}), "
              f"topic {m.get('message_thread_id') or 'General (umumiy)'}, message {m.get('message_id')}")


# ----------------------------- Small helpers --------------------------------

def esc(s):
    return html.escape(str(s), quote=False)


def isnum(x):
    try:
        return x is not None and not pd.isna(x) and np.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def money(x):
    """$65.2 mlrd, $420 mln, $1.2 trln."""
    if not isnum(x):
        return "—"
    x = float(x)
    sign = "−" if x < 0 else ""
    a = abs(x)
    if a >= 1e12:
        s = f"${a / 1e12:.2f} trln"
    elif a >= 1e10:
        s = f"${a / 1e9:.0f} mlrd"
    elif a >= 1e9:
        s = f"${a / 1e9:.1f} mlrd"
    elif a >= 1e7:
        s = f"${a / 1e6:.0f} mln"
    elif a >= 1e6:
        s = f"${a / 1e6:.1f} mln"
    else:
        s = f"${a:,.0f}"
    return sign + s


def eps_fmt(x):
    if not isnum(x):
        return "—"
    return f"{'−' if x < 0 else ''}${abs(float(x)):.2f}"


def pct(x, digits=0):
    if not isnum(x):
        return "—"
    return f"{'+' if x >= 0 else '−'}{abs(float(x)):.{digits}f}%"


def uz_date(d):
    return f"{d.day} {OYLAR[d.month - 1]}"


def uz_full(d):
    return f"{d.day} {OYLAR[d.month - 1]} {d.year}, {KUNLAR[d.weekday()]}"


def short_name(name, n=26):
    name = re.sub(r",?\s+(Inc|Corp|Corporation|Co|Company|Ltd|Limited|PLC|Holdings|Group|Class [A-C])\b\.?", "",
                  str(name or ""), flags=re.I)
    name = re.sub(r"\s+and$", "", name.strip(" ,."), flags=re.I).strip(" ,.")
    return name if len(name) <= n else name[:n - 1].rstrip() + "…"


def dot_for(est, year_ago, flat=0.02):
    """🟢 expected higher than a year ago, 🔴 lower, 🟡 about the same or unknown."""
    if not (isnum(est) and isnum(year_ago)):
        return YELLOW
    est, year_ago = float(est), float(year_ago)
    base = max(abs(year_ago), 0.01)
    d = (est - year_ago) / base
    return GREEN if d > flat else RED if d < -flat else YELLOW


def prev_trading_day(d):
    d = d - timedelta(days=1)
    while d.weekday() >= 5 or d.strftime("%Y-%m-%d") in NYSE_HOLIDAYS:
        d -= timedelta(days=1)
    return d


# ----------------------------- Earnings data --------------------------------

def to_ny(ts):
    """Yahoo time -> (date, hour, minute) in New York. A bare UTC midnight means 'date only'."""
    try:
        t = pd.Timestamp(ts)
    except Exception:
        return None
    if pd.isna(t):
        return None
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    if t.hour == 0 and t.minute == 0:
        return t.date(), None, None
    n = t.tz_convert(NY_TZ)
    return n.date(), n.hour, n.minute


def session_of(timing, hour, minute):
    tm = str(timing or "").strip().upper()
    if tm == "BMO":
        return "BMO"
    if tm == "AMC":
        return "AMC"
    if hour is None:
        return "TBD"
    m = hour * 60 + minute
    if m < 9 * 60 + 30:
        return "BMO"
    if m >= 16 * 60:
        return "AMC"
    return "TBD"


OTC_RE = re.compile(r"^[A-Z]{4}[FY]$")          # OTC foreign shares (MSBHF, MTSUY, NONOF): not on NYSE / Nasdaq


def fetch_yahoo(start, end, min_cap, pages=3):
    """Yahoo earnings calendar for start..end (dates), largest companies first."""
    import yfinance as yf
    rows = []
    cal = yf.Calendars(start=start.strftime("%Y-%m-%d"), end=(end + timedelta(days=1)).strftime("%Y-%m-%d"))
    for k in range(pages):
        df = cal.get_earnings_calendar(market_cap=min_cap, filter_most_active=False, limit=100, offset=k * 100,
                                       force=True)
        if df is None or df.empty:
            break
        for sym, r in df.iterrows():
            when = to_ny(r.get("Event Start Date"))
            if not when:
                continue
            d, h, m = when
            if not (start <= d <= end):
                continue
            if OTC_RE.match(str(sym)):
                continue
            cap = r.get("Marketcap")
            rows.append({"sym": str(sym), "name": r.get("Company") or str(sym), "cap": float(cap) if isnum(cap) else 0.0,
                         "date": d, "session": session_of(r.get("Timing"), h, m),
                         "eps_est": float(r["EPS Estimate"]) if isnum(r.get("EPS Estimate")) else None,
                         "eps_act": float(r["Reported EPS"]) if isnum(r.get("Reported EPS")) else None,
                         "surprise": float(r["Surprise(%)"]) if isnum(r.get("Surprise(%)")) else None})
        if len(df) < 100:
            break
    seen, out = set(), []
    for r in sorted(rows, key=lambda x: -x["cap"]):
        if (r["sym"], r["date"]) in seen:
            continue
        seen.add((r["sym"], r["date"]))
        out.append(r)
    # Yahoo does not always apply the market-cap filter itself: apply it here
    known = [r for r in out if r["cap"] > 0]
    if known:
        big = [r for r in out if r["cap"] >= min_cap]
        print(f"Yahoo: {len(out)} reports, {len(big)} of them >= ${min_cap / 1e9:.0f}B")
        out = big
    else:
        print("Yahoo: no market caps in the calendar, cap filter skipped")
    return out


# ----------------------------- Finnhub --------------------------------------

def fh_get(path, **params):
    """Finnhub REST call (free key: 60 calls a minute). The key is never printed."""
    url = f"https://finnhub.io/api/v1/{path}?" + urllib.parse.urlencode({**params, "token": FINNHUB_KEY})
    for k in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "TradingLab/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(4 + 4 * k)
                continue
            raise RuntimeError(f"Finnhub {path}: HTTP {e.code}")
    raise RuntimeError(f"Finnhub {path}: rate limit")


def _pos(x):
    return float(x) if isnum(x) and float(x) > 0 else None


def fetch_finnhub(start, end):
    """{(symbol, date): {...}} from Finnhub's earnings calendar."""
    data = fh_get("calendar/earnings", **{"from": start.isoformat(), "to": end.isoformat()})
    out = {}
    for x in data.get("earningsCalendar") or []:
        try:
            d = date.fromisoformat(str(x.get("date")))
        except ValueError:
            continue
        sym = str(x.get("symbol") or "").strip().replace(".", "-")
        if not sym or OTC_RE.match(sym):
            continue
        hour = str(x.get("hour") or "").lower()
        out[(sym, d)] = {"session": "BMO" if hour == "bmo" else "AMC" if hour == "amc" else "TBD",
                         "eps_est": float(x["epsEstimate"]) if isnum(x.get("epsEstimate")) else None,
                         "eps_act": float(x["epsActual"]) if isnum(x.get("epsActual")) else None,
                         "rev_est": _pos(x.get("revenueEstimate")), "rev_act": _pos(x.get("revenueActual")),
                         "quarter": x.get("quarter"), "year": x.get("year")}
    return out


def _surprise(act, est):
    return (act - est) / abs(est) * 100 if isnum(act) and isnum(est) and est else None


def merge_finnhub(r, f):
    """Finnhub numbers win; Yahoo keeps the name, market cap and anything Finnhub does not have."""
    for k in ("eps_est", "eps_act"):
        if f[k] is not None:
            r[k] = f[k]
    r["rev_est"], r["rev_act"] = f["rev_est"], f["rev_act"]
    r["quarter"], r["year"] = f["quarter"], f["year"]
    if f["session"] != "TBD":
        r["session"] = f["session"]
    sp = _surprise(r.get("eps_act"), r.get("eps_est"))
    if sp is not None:
        r["surprise"] = sp


def caps_for(rows, deadline):
    """Market caps one by one (only when Yahoo's calendar is down)."""
    import yfinance as yf

    def cap(r):
        if time.time() > deadline:
            return 0.0
        try:
            fi = yf.Ticker(r["sym"]).fast_info
            return float(fi["market_cap"]) if isnum(fi["market_cap"]) else 0.0
        except Exception:
            return 0.0

    with ThreadPoolExecutor(max_workers=6) as ex:
        for r, c in zip(rows, ex.map(cap, rows)):
            r["cap"] = c


def fetch_earnings(start, end, min_cap, pages=3):
    """Companies reporting from start to end, largest first: Yahoo gives the universe (name, market cap),
    Finnhub the expected EPS / revenue and the before / after-market flag. Either one can back up the other."""
    yh = []
    try:
        yh = fetch_yahoo(start, end, min_cap, pages)
    except Exception as e:
        print(f"Yahoo calendar: {type(e).__name__}: {e}")
    fh = {}
    if FINNHUB_KEY:
        try:
            fh = fetch_finnhub(start, end)
        except Exception as e:
            print(f"Finnhub calendar: {e}")
    else:
        print("FINNHUB_KEY topilmadi: faqat Yahoo ma'lumoti ishlatiladi")
    if not fh:
        return yh
    SOURCE["finnhub"] = True
    if yh:
        for r in yh:
            f = fh.get((r["sym"], r["date"]))
            if f:
                merge_finnhub(r, f)
        return yh
    # Yahoo's calendar is down: take the biggest revenue estimates from Finnhub and look the market caps up
    print("Yahoo kalendari yo'q: Finnhub ro'yxati ishlatiladi")
    cand = sorted(fh.items(), key=lambda kv: -(kv[1]["rev_est"] or kv[1]["rev_act"] or 0))[:MAX_DETAIL * 2]
    rows = []
    for (sym, d), f in cand:
        r = {"sym": sym, "name": sym, "cap": 0.0, "date": d, "session": f["session"], "eps_est": None,
             "eps_act": None, "surprise": None}
        merge_finnhub(r, f)
        rows.append(r)
    caps_for(rows, time.time() + 90)
    rows = [r for r in rows if r["cap"] >= min_cap or r["cap"] == 0.0]
    return sorted(rows, key=lambda r: (-r["cap"], -(r.get("rev_est") or r.get("rev_act") or 0)))


def fh_year_ago(r):
    """(EPS, revenue) this company reported for the same quarter a year ago, from Finnhub."""
    if not FINNHUB_KEY:
        return None, None
    t = r["date"] - timedelta(days=365)
    data = fh_get("calendar/earnings", symbol=r["sym"].replace("-", "."),
                  **{"from": (t - timedelta(days=50)).isoformat(), "to": (t + timedelta(days=50)).isoformat()})
    best = None
    for x in data.get("earningsCalendar") or []:
        try:
            d = date.fromisoformat(str(x.get("date")))
        except ValueError:
            continue
        if not isnum(x.get("epsActual")):
            continue
        same_q = bool(r.get("quarter")) and x.get("quarter") == r.get("quarter") and x.get("year") == (r.get("year") or 0) - 1
        key = (0 if same_q else 1, abs((d - t).days))
        if best is None or key < best[0]:
            best = (key, x)
    if not best:
        return None, None
    return float(best[1]["epsActual"]), _pos(best[1].get("revenueActual"))


def _closest_row(df, col, target):
    """Row of an estimate table whose average is closest to the calendar's EPS estimate."""
    if df is None or len(df) == 0 or col not in df.columns:
        return None
    best, gap = None, None
    for idx in df.index:
        v = df.loc[idx, col]
        if not isnum(v):
            continue
        g = abs(float(v) - float(target)) if isnum(target) else (0 if str(idx) == "0q" else 9e9)
        if gap is None or g < gap:
            best, gap = idx, g
    return best


def enrich(r):
    """Add expected revenue, year-ago numbers and approximate net income for one company."""
    import yfinance as yf
    out = {"rev_est": r.get("rev_est"), "rev_ya": None, "eps_ya": None, "shares": None, "net_est": None,
           "analysts": None}
    try:
        out["eps_ya"], out["rev_ya"] = fh_year_ago(r)
    except Exception as e:
        print(f"{r['sym']}: Finnhub year-ago: {e}")
    t = yf.Ticker(r["sym"])
    # 1) this report's own consensus (Yahoo "calendar"): valid only if its date is the report day
    try:
        cal = t.calendar or {}
        dates = cal.get("Earnings Date") or []
        if any(pd.Timestamp(x).date() == r["date"] for x in dates):
            if out["rev_est"] is None and isnum(cal.get("Revenue Average")):
                out["rev_est"] = float(cal["Revenue Average"])
            if r["eps_est"] is None and isnum(cal.get("Earnings Average")):
                r["eps_est"] = float(cal["Earnings Average"])
    except Exception:
        pass
    # 2) year-ago figures and growth: the estimate row that matches this report's EPS
    try:
        ee = t.earnings_estimate
        row = _closest_row(ee, "avg", r["eps_est"])
        if row is not None:
            if out["eps_ya"] is None and isnum(ee.loc[row, "yearAgoEps"]):
                out["eps_ya"] = float(ee.loc[row, "yearAgoEps"])
            if isnum(ee.loc[row, "numberOfAnalysts"]):
                out["analysts"] = int(ee.loc[row, "numberOfAnalysts"])
            if r["eps_est"] is None and isnum(ee.loc[row, "avg"]) and str(row) == "0q":
                r["eps_est"] = float(ee.loc[row, "avg"])
        re_ = t.revenue_estimate
        if re_ is not None and len(re_) and row is not None and row in re_.index:
            if out["rev_ya"] is None and isnum(re_.loc[row, "yearAgoRevenue"]):
                out["rev_ya"] = float(re_.loc[row, "yearAgoRevenue"])
            if out["rev_est"] is None and isnum(re_.loc[row, "avg"]):
                out["rev_est"] = float(re_.loc[row, "avg"])
    except Exception:
        pass
    # 3) shares for the approximate net income = expected EPS x shares
    try:
        fi = t.fast_info
        sh = fi["shares"] if "shares" in fi else None
        if not isnum(sh) and isnum(fi["market_cap"]) and isnum(fi["last_price"]) and fi["last_price"]:
            sh = fi["market_cap"] / fi["last_price"]
        if isnum(sh):
            out["shares"] = float(sh)
    except Exception:
        pass
    if isnum(r["eps_est"]) and out["shares"]:
        out["net_est"] = r["eps_est"] * out["shares"]
    return out


def enrich_all(rows, deadline):
    top = rows[:MAX_DETAIL]

    def work(r):
        if time.time() > deadline:
            return {}
        try:
            return enrich(r)
        except Exception as e:
            print(f"{r['sym']}: {e}")
            return {}

    with ThreadPoolExecutor(max_workers=6) as ex:
        for r, extra in zip(top, ex.map(work, top)):
            r.update(extra)
    return rows


# ----------------------------- Macro events ---------------------------------

# (keyword, Uzbek name, importance 2 = high / 1 = medium); first match wins
MACRO_MAP = [
    ("fomc", "Fed (FOMC) yig'ilishi", 2), ("fed interest rate", "Fed foiz stavkasi qarori", 2),
    ("interest rate decision", "Fed foiz stavkasi qarori", 2), ("fed chair", "Fed raisi nutqi", 2),
    ("core cpi", "Asosiy CPI (inflyatsiya)", 2), ("cpi", "CPI (inflyatsiya)", 2),
    ("core pce", "Asosiy PCE narx indeksi", 2), ("pce", "PCE narx indeksi", 2),
    ("nonfarm", "Qishloq xo'jaligidan tashqari bandlik (NFP)", 2), ("non-farm", "Qishloq xo'jaligidan tashqari bandlik (NFP)", 2),
    ("unemployment rate", "Ishsizlik darajasi", 2), ("gdp", "YaIM (GDP)", 2), ("retail sales", "Chakana savdo", 2),
    ("ppi", "PPI (ishlab chiqaruvchi narxlari)", 1), ("producer price", "PPI (ishlab chiqaruvchi narxlari)", 1),
    ("ism manufacturing", "ISM ishlab chiqarish indeksi", 1), ("ism services", "ISM xizmatlar indeksi", 1),
    ("ism non-manufacturing", "ISM xizmatlar indeksi", 1),
    ("jobless claims", "Ishsizlik bo'yicha haftalik arizalar", 1), ("jolts", "Bo'sh ish o'rinlari (JOLTS)", 1),
    ("job openings", "Bo'sh ish o'rinlari (JOLTS)", 1), ("adp", "ADP xususiy sektor bandligi", 1),
    ("consumer sentiment", "Iste'molchi kayfiyati (Michigan)", 1), ("consumer confidence", "Iste'molchi ishonchi", 1),
    ("durable goods", "Uzoq muddatli tovarlar buyurtmalari", 1), ("housing starts", "Yangi uy-joy qurilishi", 1),
    ("existing home sales", "Ikkilamchi uy-joy savdosi", 1), ("new home sales", "Yangi uy-joy savdosi", 1),
    ("crude oil inventories", "Neft zaxiralari (EIA)", 1),
]


def macro_label(event):
    e = str(event).lower()
    for key, uz, imp in MACRO_MAP:
        if key in e:
            return uz, imp
    return None


def fetch_macro(start, end):
    """Important US economic events, newest first by time. Returns [{day, hour, minute, name, imp, ...}]."""
    import yfinance as yf
    cal = yf.Calendars(start=start.strftime("%Y-%m-%d"), end=(end + timedelta(days=1)).strftime("%Y-%m-%d"))
    out = []
    for k in range(2):
        df = cal.get_economic_events_calendar(limit=100, offset=k * 100, force=True)
        if df is None or df.empty:
            break
        for ev, r in df.iterrows():
            if str(r.get("Region", "")).upper() not in ("US", "USA"):
                continue
            lab = macro_label(ev)
            if not lab:
                continue
            ts = pd.Timestamp(r.get("Event Time"))
            if pd.isna(ts):
                continue
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            ny = ts.tz_convert(NY_TZ)
            if not (start <= ny.date() <= end):
                continue
            out.append({"name": lab[0], "raw": str(ev), "imp": lab[1], "ny": ny,
                        "uz": ts.tz_convert(UZ_TZ), "period": r.get("Period"),
                        "actual": r.get("Actual"), "expected": r.get("Expected"), "last": r.get("Last")})
        if len(df) < 100:
            break
    seen, uniq = set(), []
    for m in sorted(out, key=lambda m: m["ny"]):
        key = (m["raw"], m["ny"])
        if key not in seen:
            seen.add(key)
            uniq.append(m)
    return uniq


def macro_value(v):
    return "—" if not isnum(v) else (f"{float(v):,.2f}".rstrip("0").rstrip(".") if abs(float(v)) < 1e6 else f"{float(v):,.0f}")


# ----------------------------- Card -----------------------------------------

SESSION_TITLE = {"pre": ("Ertalabki hisobotlar", "Bozor ochilishidan oldin", "pre-market"),
                 "post": ("Kechki hisobotlar", "Bozor yopilgach", "after-market")}


def make_card(day, mode, items, total, bottom_title, bottom):
    """One session on the card: up to 12 companies in two columns, then macro events (morning)
    or this morning's results (evening)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import FancyBboxPatch

    T = dict(B.THEME)
    plt.rcParams["font.family"] = B._font()
    plt.rcParams["text.parse_math"] = False        # "$" is a price here, not a formula
    title, ptitle, psub = SESSION_TITLE[mode]
    items = items[:MAX_PER_SIDE_CARD * 2]
    rows = max((len(items) + 1) // 2, 1)
    RH = 1.12
    bot = bottom[:5]
    W = 10
    H = 1.75 + 0.6 + rows * RH + (0.75 + len(bot) * 0.46 if bot else 0) + 0.7
    fig = plt.figure(figsize=(W, H), dpi=160, facecolor=T["bg"])
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_axis_off()
    ax.set_xlim(0, 1)
    ax.set_ylim(0, H)

    def panel(x, y, w, h):
        ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0,rounding_size=0.025",
                                    facecolor=T["panel"], edgecolor=T["line"], linewidth=1))

    dcol = {GREEN: T["up"], RED: T["down"], YELLOW: T["wait"]}
    ax.text(0.04, H - 0.55, title, fontsize=24, fontweight="bold", color=T["ink"], va="center")
    ax.text(0.04, H - 0.98, f"{uz_full(day)} · AQSh bozori vaqti bilan", fontsize=10, color=T["muted"], va="center")
    ax.text(0.955, H - 0.55, f"{total} ta kompaniya", fontsize=15, fontweight="bold", color=T["ink"], va="center",
            ha="right", bbox=dict(boxstyle="round,pad=0.45,rounding_size=0.6", facecolor=T["panel"],
                                  edgecolor=T["line"], linewidth=1.4))
    ax.text(0.955, H - 1.05, f"kapitalizatsiya ${MIN_CAP / 1e9:.0f} mlrd dan yuqori", fontsize=9.5, color=T["muted"],
            va="center", ha="right")
    lx = 0.04
    for dot_, txt in ((T["up"], "kutilma o'tgan yildan yuqori"), (T["down"], "past"), (T["wait"], "deyarli o'zgarmagan")):
        ax.text(lx, H - 1.4, "●", fontsize=11, color=dot_, va="center")
        ax.text(lx + 0.022, H - 1.4, txt, fontsize=8.5, color=T["muted"], va="center")
        lx += 0.05 + 0.0062 * len(txt)

    top = H - 1.75
    panel(0.03, top - 0.6 - rows * RH - 0.05, 0.95, 0.6 + rows * RH)
    ax.text(0.048, top - 0.3, ptitle, fontsize=13, fontweight="bold", color=T["ink"], va="center")
    ax.text(0.962, top - 0.3, f"{total} ta · {psub}", fontsize=9, color=T["muted"], va="center", ha="right")
    ax.plot([0.505, 0.505], [top - 0.6 - rows * RH + 0.05, top - 0.55], color=T["line"], linewidth=0.8)
    if not items:
        ax.text(0.048, top - 0.9, "Yirik kompaniyalar yo'q", fontsize=10, color=T["muted"], va="center")
    for k, r in enumerate(items):
        j, col = k // 2, k % 2                       # row by row: the largest two on top
        x0, w = (0.03, 0.4625) if col == 0 else (0.5175, 0.4625)
        y = top - 0.6 - j * RH
        if j:
            ax.plot([x0 + 0.015, x0 + w - 0.015], [y + 0.04, y + 0.04], color=T["line"], linewidth=0.8)
        d = dot_for(r["eps_est"], r.get("eps_ya"))
        ax.text(x0 + 0.018, y - 0.22, "●", fontsize=11, color=dcol[d], va="center")
        ax.text(x0 + 0.045, y - 0.22, r["sym"], fontsize=12.5, fontweight="bold", color=T["ink"], va="center")
        nm = short_name(r["name"], 22)
        if nm != r["sym"]:
            ax.text(x0 + w - 0.018, y - 0.22, nm, fontsize=8.5, color=T["muted"], va="center", ha="right")
        if isnum(r.get("eps_act")) and isnum(r.get("eps_est")):
            beat = r["eps_act"] - r["eps_est"]
            c_ = T["up"] if beat > 0.005 else T["down"] if beat < -0.005 else T["wait"]
            ax.text(x0 + 0.018, y - 0.5, f"E'lon qilindi: EPS {eps_fmt(r['eps_act'])}", fontsize=9.5,
                    fontweight="bold", color=c_, va="center")
            ax.text(x0 + w - 0.018, y - 0.5, f"kutilgan {eps_fmt(r['eps_est'])}", fontsize=8.8, color=T["muted"],
                    va="center", ha="right")
        else:
            ax.text(x0 + 0.018, y - 0.5, f"EPS {eps_fmt(r['eps_est'])}", fontsize=10, color=T["ink"], va="center")
            ax.text(x0 + w * 0.42, y - 0.5, f"Daromad {money(r.get('rev_est'))}", fontsize=10, color=T["ink"],
                    va="center")
        sub1 = f"Sof foyda ≈ {money(r.get('net_est'))}" if isnum(r.get("net_est")) else ""
        if isnum(r.get("rev_est")) and isnum(r.get("rev_ya")) and r["rev_ya"]:
            sub1 += ("  ·  " if sub1 else "") + f"daromad yillik {pct((r['rev_est'] / r['rev_ya'] - 1) * 100)}"
        if isnum(r["cap"]) and r["cap"]:
            sub1 += ("  ·  " if sub1 else "") + f"kap. {money(r['cap'])}"
        ax.text(x0 + 0.018, y - 0.8, sub1, fontsize=8, color=T["muted"], va="center")

    y = top - 0.6 - rows * RH - 0.05
    if bot:
        ax.text(0.04, y - 0.3, bottom_title, fontsize=11, color=T["muted"], va="center")
        panel(0.03, y - 0.75 - len(bot) * 0.46 + 0.1, 0.95, len(bot) * 0.46 + 0.12)
        for j, m in enumerate(bot):
            yy = y - 0.75 - j * 0.46 + 0.02
            if "ny" in m:                            # a macro event
                ax.text(0.05, yy, f"{m['ny']:%H:%M}", fontsize=10, fontweight="bold", color=T["ink"], va="center")
                ax.text(0.115, yy, "NY", fontsize=8, color=T["muted"], va="center")
                ax.text(0.15, yy, ("★ " if m["imp"] == 2 else "") + m["name"], fontsize=10.2,
                        fontweight="bold" if m["imp"] == 2 else "normal", color=T["ink"], va="center")
                ax.text(0.95, yy, f"{m['uz']:%H:%M} Toshkent" + (" (ertasi kuni)" if m["uz"].date() != m["ny"].date()
                                                                 else ""), fontsize=8.8, color=T["muted"], va="center",
                        ha="right")
            else:                                    # a company that has already reported
                beat = m["eps_act"] - m["eps_est"] if isnum(m.get("eps_est")) else 0
                c_ = T["up"] if beat > 0.005 else T["down"] if beat < -0.005 else T["wait"]
                ax.text(0.05, yy, "●", fontsize=11, color=c_, va="center")
                ax.text(0.075, yy, m["sym"], fontsize=10.5, fontweight="bold", color=T["ink"], va="center")
                ax.text(0.2, yy, f"EPS {eps_fmt(m['eps_act'])}  ·  kutilgan {eps_fmt(m.get('eps_est'))}",
                        fontsize=10, color=T["ink"], va="center")
                sp_ = m.get("surprise") if isnum(m.get("surprise")) else _surprise(m["eps_act"], m.get("eps_est"))
                ax.text(0.95, yy, pct(sp_, 1) if sp_ is not None else "", fontsize=10, fontweight="bold", color=c_,
                        va="center", ha="right")
    ax.text(0.04, 0.3, "Kutilmalar — tahlilchilar o'rtachasi, kalendar — savdo signali emas", fontsize=8,
            color=T["muted"], va="center")
    ax.text(0.96, 0.3, f"Trading Laboratory · {DISCLAIMER}", fontsize=8, color=T["muted"], va="center", ha="right")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=T["bg"])
    plt.close(fig)
    return buf.getvalue()


# ----------------------------- Text -----------------------------------------

def company_lines(r):
    d = dot_for(r["eps_est"], r.get("eps_ya"))
    nm = short_name(r["name"], 30)
    head = f"{d} <b>{esc(r['sym'])}</b>" + (f" {esc(nm)}" if nm and nm != r["sym"] else "")
    if r["cap"]:
        head += f" · {money(r['cap'])}"
    lines = [head, ""]
    if isnum(r.get("eps_act")) and isnum(r.get("eps_est")):
        beat = r["eps_act"] - r["eps_est"]
        c_ = GREEN if beat > 0.005 else RED if beat < -0.005 else YELLOW
        word = "kutilganidan yaxshi" if c_ == GREEN else "kutilganidan past" if c_ == RED else "kutilgandek"
        sp_ = f", {pct(r['surprise'], 1)}" if isnum(r.get("surprise")) else ""
        lines.append(f"{c_} E'lon qilindi: EPS <b>{eps_fmt(r['eps_act'])}</b> (kutilgan {eps_fmt(r['eps_est'])}{sp_}), {word}")
        if isnum(r.get("rev_act")):
            lines.append(f"Daromad <b>{money(r['rev_act'])}</b>" + (f" <i>(kutilgan {money(r['rev_est'])})</i>" if isnum(r.get("rev_est")) else ""))
        return lines
    parts = [f"EPS <b>{eps_fmt(r['eps_est'])}</b>"]
    if isnum(r.get("eps_ya")):
        parts[0] += f" <i>(o'tgan yil {eps_fmt(r['eps_ya'])})</i>"
    if isnum(r.get("rev_est")):
        s = f"Daromad <b>{money(r['rev_est'])}</b>"
        if isnum(r.get("rev_ya")) and r["rev_ya"]:
            s += f" <i>({pct((r['rev_est'] / r['rev_ya'] - 1) * 100)} yillik)</i>"
        parts.append(s)
    if isnum(r.get("net_est")):
        parts.append(f"Sof foyda ≈ <b>{money(r['net_est'])}</b>")
    lines.append(" · ".join(parts))
    return lines


def macro_lines(macro, title="Makro voqealar"):
    if not macro:
        return []
    out = [f"🏛 <b>{title}</b>  <i>NY vaqti / Toshkent</i>", ""]
    for m in macro[:MAX_MACRO]:
        star = "⭐ " if m["imp"] == 2 else ""
        nums = []
        if isnum(m["expected"]):
            nums.append(f"kutilma {macro_value(m['expected'])}")
        if isnum(m["last"]):
            nums.append(f"oldingisi {macro_value(m['last'])}")
        if isnum(m["actual"]):
            nums.append(f"<b>haqiqiy {macro_value(m['actual'])}</b>")
        nxt = " (ertasi kuni)" if m["uz"].date() != m["ny"].date() else ""
        out.append(f"{m['ny']:%H:%M} / {m['uz']:%H:%M}{nxt}  {star}{esc(m['name'])}"
                   + (f": <i>{' · '.join(nums)}</i>" if nums else ""))
    return out


def result_lines(rows):
    out = []
    for r in rows:
        beat = r["eps_act"] - r["eps_est"] if isnum(r.get("eps_act")) and isnum(r.get("eps_est")) else None
        c_ = YELLOW if beat is None or abs(beat) <= 0.005 else GREEN if beat > 0 else RED
        sp_ = r.get("surprise") if isnum(r.get("surprise")) else _surprise(r.get("eps_act"), r.get("eps_est"))
        sp_ = f", {pct(sp_, 1)}" if sp_ is not None else ""
        line = (f"{c_} <b>{esc(r['sym'])}</b> EPS {eps_fmt(r.get('eps_act'))} "
                f"<i>(kutilgan {eps_fmt(r.get('eps_est'))}{sp_})</i>")
        if isnum(r.get("rev_act")):
            line += f" · daromad {money(r['rev_act'])}" + (f" <i>(kutilgan {money(r['rev_est'])})</i>"
                                                           if isnum(r.get("rev_est")) else "")
        out.append(line)
    return out


def section(lines, items, head, hint):
    if not items:
        return
    lines += [head + (f"  <i>{hint}</i>" if hint else ""), ""]
    for r in items[:MAX_PER_SIDE_TEXT]:
        lines += company_lines(r)
        lines.append("")
    if len(items) > MAX_PER_SIDE_TEXT:
        rest = items[MAX_PER_SIDE_TEXT:]
        lines.append(f"<i>Yana {len(rest)} ta kichikroq kompaniya: "
                     + ", ".join(esc(r["sym"]) for r in rest[:12]) + ("…" if len(rest) > 12 else "") + "</i>")
    lines.append("")


def format_text(day, mode, groups, counts, macro, results, results_title):
    title = SESSION_TITLE[mode][0]
    lines = [f"📅 <b>{title}</b>", "", f"<i>{uz_full(day)} · AQSh bozori vaqti bilan</i>", ""]
    if mode == "pre":
        n = counts["BMO"] + counts["TBD"]
        if n:
            lines += [f"☀️ <b>{counts['BMO']}</b> ta kompaniya bozor ochilishidan oldin hisobot beradi"]
            if counts["TBD"]:
                lines.append(f"❎ {counts['TBD']} ta kompaniyaning vaqti noma'lum")
        else:
            lines.append("Bugun ertalab kapitalizatsiyasi $2 mlrd dan yuqori kompaniyalar hisobot bermaydi.")
        if counts["AMC"]:
            lines += ["", f"🌙 Yana {counts['AMC']} ta kompaniya bozor yopilgach: ro'yxati NY 15:00 da alohida xabarda"]
    else:
        if counts["AMC"]:
            lines.append(f"🌙 <b>{counts['AMC']}</b> ta kompaniya bozor yopilgach hisobot beradi")
        else:
            lines.append("Bugun kechqurun kapitalizatsiyasi $2 mlrd dan yuqori kompaniyalar hisobot bermaydi.")
    lines += ["", "🟢 kutilgan EPS o'tgan yildagidan yuqori", "🔴 past", "🟡 deyarli o'zgarmagan", ""]

    if mode == "pre":
        section(lines, groups["BMO"], "☀️ <b>Bozor ochilishidan oldin</b>", "NY 04:00–09:30")
        section(lines, groups["TBD"], "❓ <b>Vaqti aniqlanmagan yoki seans paytida</b>", "")
        ml = macro_lines(macro)
        if ml:
            lines += ml + [""]
    else:
        section(lines, groups["AMC"], "🌙 <b>Bozor yopilgach</b>", "NY 16:00 dan keyin")
    if results:
        lines += [results_title, ""] + result_lines(results) + [""]
    lines.append("⚠️ <i>Hisobot kunida narx keskin sakrashi mumkin. Warren AI risk tekshiruvi yaqin hisobotni "
                 "ogohlantiradi: bunday aksiyada yangi pozitsiya ochishdan oldin shuni hisobga oling.</i>")
    lines.append("")
    lines.append("<i>EPS va daromad — tahlilchilar kutilmasining o'rtachasi (" + ("Finnhub" if SOURCE["finnhub"] else "Yahoo Finance") + "). Sof foyda taxminiy: "
                 "kutilgan EPS × aksiyalar soni. Hisobot vaqtlari o'zgarishi mumkin. " + DISCLAIMER + "</i>")
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines))          # never two blank lines in a row


def format_week(monday, rows, macro):
    lines = ["🗓 <b>Hafta ko'rinishi</b>", f"<i>{uz_date(monday)} – {uz_date(monday + timedelta(days=4))}, "
                                            f"kapitalizatsiya ${WEEK_MIN_CAP / 1e9:.0f} mlrd dan yuqori kompaniyalar</i>", ""]
    for k in range(5):
        d = monday + timedelta(days=k)
        if d.strftime("%Y-%m-%d") in NYSE_HOLIDAYS:
            lines += [f"<b>{KUNLAR[d.weekday()].capitalize()}, {uz_date(d)}</b>  <i>bozor yopiq</i>", ""]
            continue
        day_rows = [r for r in rows if r["date"] == d]
        head = f"<b>{KUNLAR[d.weekday()].capitalize()}, {uz_date(d)}</b>"
        if not day_rows:
            lines += [head + "  <i>yirik hisobotlar yo'q</i>", ""]
            continue
        lines.append(head + f"  <i>{len(day_rows)} ta</i>")
        for key, icon in (("BMO", "☀️"), ("AMC", "🌙"), ("TBD", "❎")):
            names = [r for r in day_rows if r["session"] == key][:6]
            if names:
                lines.append(f"{icon} " + ", ".join(f"<b>{esc(r['sym'])}</b>" for r in names))
        lines.append("")
    hi = [m for m in macro if m["imp"] == 2]
    if hi:
        lines.append("⭐ <b>Haftaning asosiy makro voqealari</b>  <i>NY vaqti</i>")
        for m in hi[:10]:
            lines.append(f"{KUNLAR[m['ny'].weekday()][:3].capitalize()} {m['ny']:%H:%M}  {esc(m['name'])}")
        lines.append("")
    lines.append("<i>Har kuni ikki xabar keladi: NY 03:00 da ertalabki, NY 15:00 da kechki hisobotlar. " + DISCLAIMER + "</i>")
    return "\n".join(lines)


# ----------------------------- Main -----------------------------------------

def group_by_session(rows):
    g = {"BMO": [], "AMC": [], "TBD": []}
    for r in rows:
        g[r["session"]].append(r)
    return g


def run_time(mode, day):
    """NY time of each post: 1 hour before the pre-market (04:00) / the after-market (16:00, 13:00 on short days)."""
    if mode == "pre":
        return RUN_PRE_NY
    return (12, 0) if day.strftime("%Y-%m-%d") in NYSE_EARLY_CLOSE else RUN_POST_NY


def main():
    if not BOT_TOKEN:
        raise SystemExit("XATO: EARN_BOT_TOKEN secret topilmadi — Settings → Secrets and variables → Actions")
    now = S.ny_now()
    today = now.date()
    if DAY_OVERRIDE:
        today = datetime.strptime(DAY_OVERRIDE, "%Y-%m-%d").date()
    mode = MODE if MODE in ("pre", "post") else ("pre" if DAY_OVERRIDE or now.hour < 10 else "post")
    key = today.strftime("%Y-%m-%d")
    if not (FORCE or DAY_OVERRIDE):
        h, m = run_time(mode, today)
        target = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if now.weekday() >= 5 or key in NYSE_HOLIDAYS or not (
                -5 <= (now - target).total_seconds() / 60 <= RUN_WINDOW_MIN):
            print(f"New York {now:%Y-%m-%d %H:%M} ({mode}): not the time, nothing to do")
            return
    if today.weekday() >= 5 or key in NYSE_HOLIDAYS:
        print(f"{key}: market closed, nothing to post")
        return
    deadline = time.time() + 600
    day = datetime.combine(today, datetime.min.time())

    rows = fetch_earnings(today, today, MIN_CAP)
    print(f"{key} ({mode}): {len(rows)} reports with market cap >= ${MIN_CAP / 1e9:.0f}B")
    groups = group_by_session(rows)
    counts = {k: len(v) for k, v in groups.items()}
    focus = groups["BMO"] + groups["TBD"] if mode == "pre" else groups["AMC"]
    enrich_all(focus, deadline)                    # estimates only for the companies of this post

    macro = []
    if mode == "pre":
        try:
            macro = fetch_macro(today, today)
        except Exception as e:
            print(f"Macro calendar: {e}")
        ydate = prev_trading_day(today)
        try:
            results = [r for r in fetch_earnings(ydate, ydate, 10_000_000_000) if isnum(r.get("eps_act"))][:8]
        except Exception as e:
            print(f"Yesterday: {e}")
            results = []
        results_title = f"📈 <b>Kecha natijalari</b>  <i>{uz_date(ydate)}, eng yirik kompaniyalar:</i>"
        card_bottom, card_bottom_title = macro, "Bugungi makro voqealar"
        n = counts["BMO"] + counts["TBD"]
        caption = (f"📅 <b>Ertalabki hisobotlar</b> · {uz_date(today)}\n\n"
                   f"☀️ <b>{counts['BMO']}</b> ta kompaniya bozor ochilishidan oldin"
                   + (f"\n❎ {counts['TBD']} ta vaqti noma'lum" if counts["TBD"] else "")
                   + (f"\n\n🏛 Makro voqealar: {len(macro)} ta" if macro else "")
                   + (f"\n🌙 Bozor yopilgach: {counts['AMC']} ta, NY 15:00 da" if counts["AMC"] else ""))
    else:
        results = [r for r in groups["BMO"] + groups["TBD"] if isnum(r.get("eps_act"))][:8]
        results_title = "☀️ <b>Bugun ertalab e'lon qilinganlar</b>  <i>eng yirik kompaniyalar:</i>"
        card_bottom, card_bottom_title = results, "Bugun ertalab e'lon qilinganlar"
        n = counts["AMC"]
        caption = (f"📅 <b>Kechki hisobotlar</b> · {uz_date(today)}\n\n"
                   f"🌙 <b>{counts['AMC']}</b> ta kompaniya bozor yopilgach"
                   + (f"\n\n☀️ Ertalab e'lon qilinganlar: {len(results)} ta" if results else ""))

    png = make_card(day, mode, focus, n, card_bottom_title, card_bottom) if (n or card_bottom) else None
    send(png, caption, format_text(day, mode, groups, counts, macro, results, results_title))

    if mode == "pre" and today.weekday() == 0:      # Monday morning: the week ahead
        week_end = today + timedelta(days=4)
        try:
            wrows = fetch_earnings(today, week_end, WEEK_MIN_CAP)
            wmacro = fetch_macro(today, week_end)
            send(None, "", format_week(today, wrows, wmacro))
        except Exception as e:
            print(f"Week overview: {e}")
    print(f"Sent ({mode}): {n} reports, {len(macro)} macro events, {len(results)} results")


if __name__ == "__main__":
    main()
