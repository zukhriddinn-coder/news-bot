"""
Trading Laboratory -> Buffet AI daily scanner ("Kunlik nomzodlar")
Twice every trading day (started by Google Apps Script, scanner.gs):
  - "morning": 09:25 New York, candidates arrive by the 09:30 open (closed daily candles only);
  - "close":   15:00 New York, one hour before the close (12:00 on 13:00 early-close days).
  1. Universe: S&P 500 members + NASDAQ-listed common stocks with market cap >= $2B
     (NASDAQ-100 members are tagged). Sources: datasets/s-and-p-500-companies (GitHub), Nasdaq's
     screener (market caps), Wikipedia (NASDAQ-100 list). Fallbacks are built in.
  2. Daily bars for all of them (Yahoo Finance, 2 years), liquidity filter.
  3. The full Buffet AI rule on every stock (trend, divergence, candles, setups, levels, plan).
  4. LONG plans only: Time Machine (5,000 replays) + the same rule on past bars + risk gate.
  5. The best 5 that pass (no earnings inside the plan) are posted to the Buffet AI topic.
These are CANDIDATES found by rules, not buy signals.
"""

import html
import io
import json
import os
import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import numpy as np

import buffetai as B

# ---------------------------------------------------------------------------
# SETTINGS (you can edit these)
# ---------------------------------------------------------------------------

TOP_N = 5                      # candidates per day
MIN_CAP = 2e9                  # NASDAQ stocks: market cap at least $2B (S&P 500 members always included)
MIN_PRICE = 5.0
MIN_DOLLAR_VOLUME = 25e6       # $ traded per day (20-day average): keeps high-priced stocks like BKNG
RUN_WINDOW_MIN = 40            # start late at most 40 minutes (the market closes at 16:00)
TIME_BUDGET = 22 * 60          # seconds for the whole scan
MIN_EDGE = 0.05                # Time Machine: TP1 share must beat break-even by 5+ points
MAX_RSI = 80                   # skip stocks that are already extremely overbought
MAX_RISK_PCT = 12              # skip plans whose stop is more than 12% away
SP500_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
NASDAQ_SCREENER = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&download=true&exchange=nasdaq"
NASDAQ_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
WIKI_SP500 = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
WIKI_NDX = "https://en.wikipedia.org/wiki/Nasdaq-100"

# ---------------------------------------------------------------------------

CHAT_ID = os.environ.get("BUFFET_CHAT_ID", "").strip()
THREAD_ID = os.environ.get("BUFFET_THREAD_ID", "").strip()
FORCE = os.environ.get("FORCE", "").strip().lower() in ("1", "true", "yes")
MODE = (os.environ.get("MODE", "") or "close").strip().lower()   # "morning" (09:25 NY) or "close" (15:00 NY)

# NYSE calendar (from day-trader-skill's trader-alerts, nyse.com hours-calendars, read 2026-10-13)
NYSE_HOLIDAYS = {"2026-11-26", "2026-12-25", "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26",
                 "2027-05-31", "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24"}
NYSE_EARLY_CLOSE = {"2026-11-27", "2026-12-24", "2027-11-26"}      # the market closes at 13:00
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36", "Accept": "application/json, text/plain, */*",
      "Accept-Language": "en-US,en;q=0.9"}
STARTED = time.time()


def get(url, timeout=40):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def ysym(s):
    return s.strip().upper().replace(".", "-").replace("/", "-").replace(" ", "")


# ----------------------------- New York clock -------------------------------

def ny_now():
    """New York time without extra packages (US DST: 2nd Sunday of March -> 1st Sunday of November)."""
    utc = datetime.now(timezone.utc)

    def nth_sunday(year, month, n):
        d = datetime(year, month, 1, tzinfo=timezone.utc)
        d += timedelta(days=(6 - d.weekday()) % 7)
        return d + timedelta(weeks=n - 1)

    start = nth_sunday(utc.year, 3, 2).replace(hour=7)      # 2:00 EST = 7:00 UTC
    end = nth_sunday(utc.year, 11, 1).replace(hour=6)       # 2:00 EDT = 6:00 UTC
    offset = -4 if start <= utc < end else -5
    return (utc + timedelta(hours=offset)).replace(tzinfo=None)


# ----------------------------- Universe -------------------------------------

def sp500():
    try:
        import csv
        rows = list(csv.reader(io.StringIO(get(SP500_URL))))[1:]
        out = {ysym(r[0]): r[1] for r in rows if len(r) > 1}
        if len(out) > 450:
            return out
    except Exception as exc:
        print(f"S&P 500 csv: {exc}")
    import pandas as pd
    t = pd.read_html(io.StringIO(get(WIKI_SP500)))[0]
    return {ysym(s): n for s, n in zip(t["Symbol"], t["Security"])}


def nasdaq100():
    try:
        import pandas as pd
        for t in pd.read_html(io.StringIO(get(WIKI_NDX))):
            col = next((c for c in t.columns if str(c).lower() in ("ticker", "symbol")), None)
            if col is not None and len(t) > 90:
                return {ysym(s) for s in t[col]}
    except Exception as exc:
        print(f"NASDAQ-100 list: {exc}")
    return set()


def money(s):
    s = re.sub(r"[^0-9.]", "", str(s or ""))
    try:
        return float(s) if s else 0.0
    except ValueError:
        return 0.0


BAD_NAME = re.compile(r"\bwarrants?\b|\brights?\b|\bunits?\b|\bpreferred\b|depositary shares? representing|"
                      r"\bnotes? due\b|%|\betf\b|\betn\b|acquisition corp", re.I)


def nasdaq_caps():
    """NASDAQ-listed common stocks with market caps: {symbol: (name, cap)}. cap 0 = unknown."""
    try:
        data = json.loads(get(NASDAQ_SCREENER, timeout=60))
        rows = data["data"]["rows"]
        out = {}
        for r in rows:
            sym, name = ysym(r.get("symbol", "")), r.get("name", "")
            if sym and not BAD_NAME.search(name) and "^" not in sym:
                out[sym] = (name, money(r.get("marketCap")))
        if len(out) > 1500:
            print(f"Nasdaq screener: {len(out)} stocks")
            return out
    except Exception as exc:
        print(f"Nasdaq screener: {exc}")
    out = {}                                           # fallback: the symbol file, caps from Yahoo later
    try:
        listed = get(NASDAQ_LISTED).splitlines()[1:]
    except Exception as exc:
        print(f"nasdaqlisted.txt: {exc} -> S&P 500 and NASDAQ-100 only today")
        return out
    for line in listed:
        p = line.split("|")
        if len(p) < 8 or p[3] != "N" or p[6] != "N" or BAD_NAME.search(p[1]):
            continue
        out[ysym(p[0])] = (p[1], 0.0)
    print(f"nasdaqlisted.txt: {len(out)} stocks (market caps from Yahoo)")
    return out


def universe():
    sp = sp500()
    ndx = nasdaq100()
    nq = nasdaq_caps()
    stocks = {}
    for s, n in sp.items():
        stocks[s] = {"name": n, "tags": ["S&P 500"], "cap": None}
    for s, (n, cap) in nq.items():
        if cap and cap < MIN_CAP and s not in sp and s not in ndx:
            continue
        st = stocks.setdefault(s, {"name": re.sub(r"\s*-?\s*(Common Stock.*|Class [A-C].*|Ordinary Shares.*|American Depositary.*)$", "", n),
                                   "tags": [], "cap": cap or None})
        st["cap"] = st["cap"] or cap or None
        if "NASDAQ" not in st["tags"]:
            st["tags"].append("NASDAQ")
    for s in ndx:
        stocks.setdefault(s, {"name": s, "tags": [], "cap": None})
        stocks[s]["tags"].append("NASDAQ-100")
    print(f"Universe: {len(stocks)} (S&P 500 {len(sp)}, NASDAQ {len(nq)}, NASDAQ-100 {len(ndx)})")
    return stocks


# ----------------------------- Prices ---------------------------------------

def download(symbols, period="2y", min_bars=220):
    """Daily bars for many symbols, in chunks. Returns {symbol: DataFrame}."""
    import yfinance as yf
    out = {}
    syms = list(symbols)
    for i in range(0, len(syms), 120):
        part = syms[i:i + 120]
        for attempt in range(2):
            try:
                data = yf.download(part, period=period, interval="1d", group_by="ticker", auto_adjust=False,
                                   threads=True, progress=False)
                break
            except Exception as exc:
                print(f"download {i}: {exc}")
                time.sleep(10)
                data = None
        if data is None or data.empty:
            continue
        for s in part:
            try:
                cols = data.columns
                df = data[s] if getattr(cols, "nlevels", 1) > 1 and s in cols.get_level_values(0) else data
                df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
                if len(df) >= min_bars:
                    out[s] = df
            except Exception:
                pass
        time.sleep(1.5)
        print(f"  prices {min(i + 120, len(syms))}/{len(syms)}: {len(out)} usable")
    return out


def yahoo_caps(symbols):
    import yfinance as yf

    def one(s):
        try:
            return s, float(yf.Ticker(s).fast_info.get("marketCap") or 0)
        except Exception:
            return s, 0.0
    with ThreadPoolExecutor(max_workers=8) as pool:
        return dict(pool.map(one, symbols))


# ----------------------------- Ranking --------------------------------------

def evaluate(sym, df, info):
    """Buffet AI on one stock -> (said LONG?, candidate or None when a gate fails)."""
    a = B.analyse(df)
    p = a["plan"]
    if p["side"] != "LONG":
        return False, None
    if not p.get("targets") or p["targets"][0]["rr"] < 1.0:
        return True, None
    if a["rsi"] >= MAX_RSI or p["risk_pct"] > MAX_RISK_PCT:     # too hot to chase / stop too far
        return True, None
    bars = B.HORIZON["1D"]
    pool = B.bar_pool(df)
    mc = B.simulate(pool, "LONG", p["entry"], p["stop"], p["targets"][0]["price"], bars)
    kind, verdict = B.mc_verdict(mc["p_target"], mc["p_stop"], mc["r_target"])
    hit = mc["p_target"] + mc["p_stop"]
    edge = (mc["p_target"] / hit - 1 / (1 + mc["r_target"])) if hit else -1
    if edge < MIN_EDGE or mc["p_target"] <= mc["p_stop"]:     # TP1 must also come first more often
        return True, None
    mc.update(verdict_kind=kind, verdict=verdict, edge=round(edge, 3))
    risk = B.risk_gate(p, a, df, {}, "stock")
    if any(lv == "block" for lv, _ in risk):
        return True, None
    return True, {"symbol": sym, "info": info, "a": a, "mc": mc, "risk": risk, "df": df}


def rank_key(c):
    h = c.get("history") or {}
    hist = (h.get("avg_R") or 0) if h.get("fired", 0) >= 5 else 0
    a, mc = c["a"], c["mc"]
    return (mc["edge"] * 100                       # Time Machine: how far above break-even
            + mc["p_target"] * 40                  # ... and how often TP1 actually comes within 20 days
            + hist * 10                            # the same rule on past bars
            + a["score"] * 2                       # Buffet AI signal strength
            - max(0, a["plan"]["risk_pct"] - 6) * 2)   # prefer tighter stops


# ----------------------------- Output ---------------------------------------

def esc(s):
    return html.escape(str(s or ""), quote=False)


fp = B.fp


def phase(now):
    """(title, subtitle, candle note, pre_open) for this run."""
    open_t = now.replace(hour=9, minute=30, second=0, microsecond=0)
    if MODE == "morning" and now < open_t:
        return ("Bozor ochilishidan oldin", "bozor ochilishidan oldin · kechagi yopilgan kunlik sham",
                "Tahlil kechagi to'liq yopilgan shamlar bo'yicha.", True)
    if MODE == "morning":
        mins = int((now - open_t).total_seconds() // 60)
        return ("Sessiya boshida", f"sessiya ochilganidan {mins} daqiqa keyin · bugungi sham endi shakllanmoqda",
                "Bugungi sham endi shakllanmoqda: kun oxirigacha signal o'zgarishi mumkin.", False)
    short = now.strftime("%Y-%m-%d") in NYSE_EARLY_CLOSE
    return ("Yopilishga 1 soat", "bozor yopilishiga 1 soat qolganda" + (" (bugun qisqa kun, 13:00 da yopiladi)"
                                                                         if short else "") + " · kunlik grafik",
            "Bugungi sham ~85% shakllangan.", False)


def format_text(day, picks, stats):
    title, subtitle, candle_note, pre_open = phase(day)
    lines = [f"📡 <b>Buffet AI · Nomzodlar · {title}</b>",
             f"<i>{day:%d.%m.%Y} · {subtitle}</i>", ""]
    if not picks:
        lines += ["Bugun barcha shartlardan o'tgan nomzod topilmadi. Bu ham natija: majburan savdo qilmaslik "
                  "kerak bo'lgan kunlar bo'ladi.", ""]
    for k, c in enumerate(picks, 1):
        a, p, mc, info = c["a"], c["a"]["plan"], c["mc"], c["info"]
        t = p["targets"]
        setups = ", ".join(s["name"] for s in a["setups"]) or "—"
        lines += [f"{k}️⃣ <b>${esc(c['symbol'])}</b> · {esc(info['name'][:40])} · "
                  f"<i>{esc(', '.join(info['tags']))}</i>",
                  f"   Narx {fp(a['price'])} · trend: {esc(a['trend'])} · RSI {a['rsi']}",
                  f"   🔎 Setap: {esc(setups)}",
                  f"   Kirish {fp(p['entry'])} · 🛑 SL {fp(p['stop'])} ({p['risk_pct']}%) · "
                  f"🎯 TP1 {fp(t[0]['price'])} (1:{t[0]['rr']})" + (f" · TP2 {fp(t[1]['price'])}" if len(t) > 1 else ""),
                  f"   🎲 TP1 birinchi {mc['p_target'] * 100:.0f}% · stop birinchi {mc['p_stop'] * 100:.0f}% "
                  f"(20 kun ichida)"]
        h = c.get("history")
        if h and h["fired"]:
            lines.append(f"   📜 Shu qoida o'tmishda {h['fired']} marta: {h['target']} TP1 / {h['stop']} stop"
                         + (f" · {h['avg_R']:+.2f}R" if h["avg_R"] is not None else ""))
        ev = c.get("events") or {}
        if ev.get("earnings"):
            d = datetime.fromisoformat(ev["earnings"])
            if d.date() >= day.date():
                lines.append(f"   📅 Keyingi hisobot: {d:%d.%m.%Y}")
            else:
                lines.append(f"   📅 Oxirgi hisobot: {d:%d.%m.%Y} (keyingisi sanasi hali e'lon qilinmagan)")
        for lv, txt in c["risk"]:
            lines.append(f"   ⚠️ {esc(txt)}")
        if pre_open:
            lines.append(f"   ⏰ Ochilishda: {fp(p['entry'] + a['atr'])} dan yuqori bo'lsa — kutib turing (gap), "
                         f"{fp(p['stop'])} dan past bo'lsa — reja bekor")
        lines.append("")
    lines += [f"🔬 <b>Qanday tanlandi:</b> {stats['universe']} aksiya → narx ($5+) va kunlik savdo ($25M+) filtridan {stats['liquid']} ta "
              f"→ Buffet AI LONG reja {stats['long']} ta → Time Machine va risk tekshiruvidan {stats['passed']} ta → "
              f"eng yaxshi {len(picks)} ta. Hisoboti reja muddati (~4 hafta) ichida bo'lganlar chiqarib tashlangan.",
              "", "💬 Batafsil tahlil uchun shu bo'limga yozing: <b>$TIKER</b> (masalan $" +
              (picks[0]["symbol"] if picks else "AAPL") + ")", ""]
    if stats.get("partial"):
        lines += [f"<i>ℹ️ {stats['partial']}</i>", ""]
    lines.append(f"<i>⚠️ Bu qoidalar asosida topilgan NOMZODLAR, sotib olish signali emas. {candle_note} "
                 "Ehtimollar o'tmishdagi shamlarga asoslangan simulyatsiya. Investitsion maslahat emas. "
                 "Buffet AI Warren Buffett bilan bog'liq emas.</i>")
    return "\n".join(lines)


def make_card(day, picks, stats):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import FancyBboxPatch

    T = B.THEME
    plt.rcParams["font.family"] = B._font()
    n = max(len(picks), 1)
    H = 2.0 + 1.25 * n + 0.6
    fig = plt.figure(figsize=(10, H), dpi=160, facecolor=T["bg"])
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_axis_off()
    ax.set_xlim(0, 1)
    ax.set_ylim(0, H)
    title, subtitle, _, _ = phase(day)
    ax.text(0.04, H - 0.55, f"Nomzodlar · {title}", fontsize=24, fontweight="bold", color=T["ink"], va="center")
    ax.text(0.04, H - 1.0, f"Buffet AI · {day:%d.%m.%Y} · S&P 500 + NASDAQ (> $2 mlrd) · {subtitle.split(' · ')[0]}",
            fontsize=10, color=T["muted"], va="center")
    ax.text(0.96, H - 0.5, f"{stats['universe']} → {len(picks)}", fontsize=20, fontweight="bold", color=T["up"],
            va="center", ha="right")
    ax.text(0.96, H - 0.95, "aksiya tekshirildi → nomzod", fontsize=9, color=T["muted"], va="center", ha="right")
    for x, t in ((0.04, "Aksiya"), (0.34, "Oxirgi 60 kun · kirish / SL / TP1"), (0.66, "Reja"),
                 (0.955, "TP1 ehtimoli")):
        ax.text(x, H - 1.5, t, fontsize=9, color=T["muted"], va="center", ha="right" if x > 0.9 else "left")
    if not picks:
        ax.text(0.5, H - 2.4, "Bugun shartlardan o'tgan nomzod yo'q", fontsize=13, color=T["muted"], ha="center")
    for k, c in enumerate(picks):
        top = H - 1.75 - k * 1.25
        a, p, mc = c["a"], c["a"]["plan"], c["mc"]
        ax.add_patch(FancyBboxPatch((0.03, top - 1.12), 0.94, 1.08, boxstyle="round,pad=0,rounding_size=0.03",
                                    facecolor=T["panel"], edgecolor=T["line"], linewidth=1))
        ax.text(0.05, top - 0.36, f"${c['symbol']}", fontsize=16, fontweight="bold", color=T["ink"], va="center")
        ax.text(0.05, top - 0.68, c["info"]["name"][:26], fontsize=8.5, color=T["muted"], va="center")
        ax.text(0.05, top - 0.92, " · ".join(c["info"]["tags"]), fontsize=7.5, color=T["e20"], va="center")
        # sparkline with levels
        sx = fig.add_axes([0.33, (top - 1.02) / H, 0.29, 0.88 / H], facecolor=T["panel"])
        closes = c["df"]["Close"].to_numpy(dtype=float)[-60:]
        sx.plot(range(len(closes)), closes, color=T["ink"], linewidth=1.3)
        for y, col, ls in ((p["entry"], T["entry"], "-"), (p["stop"], T["sl"], "--"),
                           (p["targets"][0]["price"], T["tp"], "--")):
            sx.axhline(y, color=col, linewidth=0.9, linestyle=ls)
        lo = min(closes.min(), p["stop"])
        hi = max(closes.max(), p["targets"][0]["price"])
        sx.set_ylim(lo - (hi - lo) * 0.05, hi + (hi - lo) * 0.05)
        sx.set_axis_off()
        t0 = p["targets"][0]
        ax.text(0.66, top - 0.3, f"Kirish {fp(p['entry'])}", fontsize=9.5, color=T["ink"], va="center")
        ax.text(0.66, top - 0.56, f"SL {fp(p['stop'])}  ({p['risk_pct']}%)", fontsize=9.5, color=T["sl"], va="center")
        ax.text(0.66, top - 0.82, f"TP1 {fp(t0['price'])}  (1:{t0['rr']})", fontsize=9.5, color=T["tp"], va="center")
        ax.text(0.955, top - 0.42, f"{mc['p_target'] * 100:.0f}%", fontsize=18, fontweight="bold", color=T["up"],
                va="center", ha="right")
        ax.text(0.955, top - 0.78, f"stop {mc['p_stop'] * 100:.0f}%", fontsize=9, color=T["muted"], va="center",
                ha="right")
    ax.text(0.04, 0.3, "Nomzodlar — sotib olish signali emas · Time Machine: 5 000 replay, 20 kun",
            fontsize=8, color=T["muted"], va="center")
    ax.text(0.96, 0.3, "Trading Laboratory · Investitsion maslahat emas", fontsize=8, color=T["muted"],
            va="center", ha="right")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=T["bg"])
    plt.close(fig)
    return buf.getvalue()


def send(png, text, caption):
    B.CHAT_ID, B.THREAD_ID, B.REPLY_TO = CHAT_ID, THREAD_ID, ""
    B.send_photo(png, caption)
    B.send_text(text, reply=False, html_mode=True)


# ----------------------------- Main -----------------------------------------

def main():
    if not CHAT_ID or not B.BOT_TOKEN:
        raise SystemExit("XATO: GitHub secret topilmadi — " + ", ".join(
            n for n, v in (("BUFFET_BOT_TOKEN", B.BOT_TOKEN), ("BUFFET_CHAT_ID", CHAT_ID)) if not v)
            + " ni Settings → Secrets and variables → Actions ga qo'shing")
    now = ny_now()
    day = now.strftime("%Y-%m-%d")
    if not FORCE:
        if now.weekday() >= 5 or day in NYSE_HOLIDAYS:
            print(f"New York {day}: market closed (weekend or holiday), nothing to do")
            return
        mins = now.hour * 60 + now.minute
        if MODE == "morning":
            ok = 9 * 60 + 15 <= mins <= 10 * 60 + 30            # started at 09:25 New York (late at most ~1 hour)
        else:
            start = (12 if day in NYSE_EARLY_CLOSE else 15) * 60   # one hour before the close
            ok = start <= mins <= start + RUN_WINDOW_MIN
        if not ok:
            print(f"New York {now:%Y-%m-%d %H:%M}: not the {MODE} scan time, nothing to do")
            return
    print(f"New York {now:%Y-%m-%d %H:%M}: scanning")

    stocks = universe()
    prices = download(stocks)
    if phase(now)[3]:                                    # before the open: use only closed daily candles
        prices = {k: v[v.index.date < now.date()] for k, v in prices.items()}
        prices = {k: v for k, v in prices.items() if len(v) >= 220}
    if len(prices) < len(stocks) * 0.5:
        B.CHAT_ID, B.THREAD_ID, B.REPLY_TO = CHAT_ID, THREAD_ID, ""
        B.send_text(f"⚠️ Buffet AI kunlik skani: Yahoo Finance'dan ma'lumot yetarli yuklanmadi "
                    f"({len(stocks)} aksiyadan {len(prices)} tasi). Bugungi nomzodlar yuborilmaydi.", reply=False)
        raise SystemExit(f"only {len(prices)} of {len(stocks)} prices")

    liquid = {}
    for s, df in prices.items():
        c, v = df["Close"].to_numpy(dtype=float), df["Volume"].to_numpy(dtype=float)
        if c[-1] >= MIN_PRICE and np.mean(v[-21:-1] * c[-21:-1]) >= MIN_DOLLAR_VOLUME:
            liquid[s] = df
    unknown = [s for s in liquid if stocks[s]["cap"] is None and "S&P 500" not in stocks[s]["tags"]
               and "NASDAQ-100" not in stocks[s]["tags"]]
    if unknown:
        caps = yahoo_caps(unknown)
        for s in unknown:
            stocks[s]["cap"] = caps.get(s) or 0
            if stocks[s]["cap"] < MIN_CAP:
                liquid.pop(s, None)
    print(f"Liquid: {len(liquid)} of {len(prices)} with prices")

    cands, longs = [], 0
    for s, df in liquid.items():
        if time.time() - STARTED > TIME_BUDGET * 0.6:
            break
        try:
            is_long, c = evaluate(s, df, stocks[s])
        except Exception as exc:
            print(f"{s}: {exc}")
            continue
        longs += is_long
        if c:
            cands.append(c)
    print(f"LONG plans: {longs}, passed Time Machine + risk: {len(cands)}")

    cands.sort(key=rank_key, reverse=True)
    for c in cands[:25]:                                     # history test for the best ones only
        if time.time() - STARTED > TIME_BUDGET * 0.85:
            break
        c["history"] = B.history_test(c["df"], "LONG", B.HORIZON["1D"], deadline=time.time() + 8)
    cands = [c for c in cands if not (c.get("history") and c["history"]["fired"] >= 5
                                      and (c["history"]["avg_R"] or 0) <= 0.05)]
    cands.sort(key=rank_key, reverse=True)

    picks = []
    for c in cands:
        if len(picks) >= TOP_N:
            break
        ev = B.events(c["symbol"], "1D", c["df"])
        if ev.get("earnings_in_window"):
            print(f"{c['symbol']}: earnings {ev['earnings']} inside the plan, skipped")
            continue
        c["events"] = ev
        c["risk"] = [r for r in B.risk_gate(c["a"]["plan"], c["a"], c["df"], ev, "stock") if r[0] == "warn"]
        picks.append(c)

    stats = {"universe": len(stocks), "liquid": len(liquid), "long": longs, "passed": len(cands)}
    if len(prices) < len(stocks) * 0.8:
        stats["partial"] = (f"{len(stocks)} aksiyadan {len(prices)} tasining narxi yuklandi "
                            "(qolganlari Yahoo'da topilmadi yoki cheklov).")
    caption = (f"📡 <b>Buffet AI · Nomzodlar · {phase(now)[0]}</b> · {now:%d.%m.%Y}\n"
               + (" · ".join(f"${c['symbol']}" for c in picks) if picks else "Bugun nomzod yo'q"))
    send(make_card(now, picks, stats), format_text(now, picks, stats), caption)
    print(f"Sent {len(picks)} candidates in {time.time() - STARTED:.0f}s")


if __name__ == "__main__":
    main()
