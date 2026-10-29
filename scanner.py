"""
Trading Laboratory -> Buffet AI daily scanner ("Kunlik nomzodlar"), 5-year evidence version
Every US trading day at 15:30 New York time (30 minutes before the close) Google Apps Script starts this script.
  1. Universe: S&P 500 members + NASDAQ-listed common stocks with market cap >= $2B (NASDAQ-100 tagged).
  2. Daily bars (Yahoo Finance, 2 years); price $5+ and $25M+ traded a day.
  3. Buffet AI LONG plan on every stock (RSI < 80, TP1 >= 1:1, stop <= 12%).
  4. A candidate only when the signal matches a rule that made money in BOTH 5-year test periods
     (2021-24 found, 2025-26 checked, after costs; calibration.json, rebuilt every Saturday by calibrate.py).
     The rule's own exit (stop / take profit) is used; stocks whose own 5-year record is negative are skipped.
  5. The best 5 without earnings inside the plan are posted. Some days there are none: that is by design.
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
import rules as R

# ---------------------------------------------------------------------------
# SETTINGS (you can edit these)
# ---------------------------------------------------------------------------

TOP_N = 5                      # candidates per day
MIN_CAP = 2e9                  # NASDAQ stocks: market cap at least $2B (S&P 500 members always included)
MIN_PRICE = 5.0
MIN_DOLLAR_VOLUME = 25e6       # $ traded per day (20-day average): keeps high-priced stocks like BKNG
RUN_WINDOW_MIN = 25            # start late at most 25 minutes (the market closes at 16:00)
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
MODE = (os.environ.get("MODE", "") or "close").strip().lower()   # "morning" (10:00 NY) or "close" (15:00 NY)

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
    own = (h.get("avg_R") or 0) if h.get("enough") else 0          # this stock's 5-year record (15+ trades only)
    base = (c.get("base") or {}).get("avg_R") or 0                  # the whole market, same signal type
    a, mc = c["a"], c["mc"]
    return (mc["edge"] * 100                       # Time Machine: how far above break-even
            + mc["p_target"] * 40                  # ... and how often TP1 actually comes within 20 days
            + base * 15                            # market-wide 5-year result of this signal type
            + own * 5                              # this stock's own 5-year result
            + a["score"] * 2                       # Buffet AI signal strength
            - max(0, a["plan"]["risk_pct"] - 6) * 2)   # prefer tighter stops


def evaluate_v2(sym, df, info, approved, spy_ctx, cal):
    """v2: a candidate only when the signal matches a rule that worked in BOTH test periods.
    Returns (said LONG?, candidate or None)."""
    a = B.analyse(df)
    if a["plan"]["side"] != "LONG":
        return False, None
    if not R.basic_gate(a):
        return True, None
    f = R.features(a, df.iloc[-501:], spy_ctx)
    matches = [r for r in approved if R.rule_matches(r["rule"], f)]
    if not matches:
        return True, None
    rule = max(matches, key=lambda r: (r["disc"]["avg_R"] + r["val"]["avg_R"]) / 2)
    v = rule["variant"]
    own = ((cal or {}).get("per_stock", {}).get(sym) or {}).get(v)
    if own and own["trades"] >= B.MIN_TRADES and (own["avg_R"] or 0) <= 0:
        return True, None                                   # this stock's own 5-year record is negative
    e, st, tp = R.variant_plan(v, a)
    risk = e - st
    plan = {"side": "LONG", "entry": B.r2(e), "stop": B.r2(st), "risk_pct": round(risk / e * 100, 2),
            "targets": [{"price": B.r2(tp), "rr": round((tp - e) / risk, 1)},
                        {"price": B.r2(e + 2 * (tp - e)), "rr": round(2 * (tp - e) / risk, 1)}]}
    a = dict(a, plan=plan)
    mc = B.simulate(B.bar_pool(df), "LONG", e, st, tp, B.HORIZON["1D"])
    hit = mc["p_target"] + mc["p_stop"]
    mc["edge"] = round((mc["p_target"] / hit - 1 / (1 + mc["r_target"])) if hit else -1, 3)
    risk_notes = B.risk_gate(plan, a, df, {}, "stock")
    if any(lv == "block" for lv, _ in risk_notes):
        return True, None
    hist = (dict(own, fired=own["trades"], enough=own["trades"] >= B.MIN_TRADES) if own else None)
    return True, {"symbol": sym, "info": info, "a": a, "mc": mc, "risk": risk_notes, "df": df,
                  "rule": rule, "features": f, "history": hist}


def rank_key_v2(c):
    r, mc, h = c["rule"], c["mc"], c.get("history") or {}
    own = (h.get("avg_R") or 0) if h.get("enough") else 0
    return ((r["disc"]["avg_R"] + r["val"]["avg_R"]) / 2 * 100   # the rule's 5-year result (both periods)
            + mc["p_target"] * 20                                # Time Machine for this stock today
            + own * 5                                            # this stock's own record with this exit
            - max(0, c["a"]["plan"]["risk_pct"] - 8))            # prefer tighter stops


# ----------------------------- Output ---------------------------------------

def esc(s):
    return html.escape(str(s or ""), quote=False)


fp = B.fp


OYLAR = ["yanvar", "fevral", "mart", "aprel", "may", "iyun", "iyul", "avgust", "sentyabr", "oktyabr", "noyabr",
         "dekabr"]
MEDALS = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]


def phase(now):
    """(title, pre_open) for this run."""
    if MODE == "morning" and now < now.replace(hour=9, minute=30, second=0, microsecond=0):
        return "ochilishdan oldin", True
    if MODE == "morning":
        return "sessiya boshida", False
    return "yopilishga 30 daqiqa qolganda", False


def uz_date(d):
    return f"{d.day} {OYLAR[d.month - 1]}"


def num(x):
    """Price without the $ sign, for <code> (tap to copy in Telegram)."""
    return f"{x:,.2f}" if abs(x) >= 1 else f"{x:.4g}"


def pct(x):
    return f"{'+' if x >= 0 else '−'}{abs(x):.1f}%"


def plan_pre(p):
    """The plan as plain lines: Kirish, Stop, TP1, TP2."""
    e = p["entry"]
    rows = [f"Kirish <b>{num(e)}</b>", f"Stop <b>{num(p['stop'])}</b>  <i>{pct(p['stop'] / e * 100 - 100)}</i>"]
    rows += [f"TP{k} <b>{num(t['price'])}</b>  <i>{pct(t['price'] / e * 100 - 100)}</i>"
             for k, t in enumerate(p["targets"][:2], 1)]
    return "\n".join(rows)


def pick_lines(k, c, day):
    a, p, mc, info = c["a"], c["a"]["plan"], c["mc"], c["info"]
    out = [f"{MEDALS[k]} <b>${esc(c['symbol'])}</b>  <i>{esc(info['name'][:38])}</i>", plan_pre(p),
           f"🟢 TP1 birinchi <b>{mc['p_target'] * 100:.0f}%</b>, stop birinchi {mc['p_stop'] * 100:.0f}%"
           f"  <i>(1:{p['targets'][0]['rr']})</i>"]
    ru = c.get("rule")
    if ru:
        d, v = ru["disc"], ru["val"]
        out += [f"📚 Qoida: <i>{esc(R.human(ru['rule']))}</i>",
                f"📊 5 yil: 2021–24 {d['trades']} savdo, <b>{d['avg_R']:+.2f}R</b>. 2025–26 {v['trades']} savdo, "
                f"<b>{v['avg_R']:+.2f}R</b>"]
    h = c.get("history")
    if h and h.get("fired"):
        out.append(f"📜 Shu aksiyada 5 yil: {h['fired']} savdo, {h['target']} TP1 / {h['stop']} stop"
                   + (f", <b>{h['avg_R']:+.2f}R</b>" if h.get("avg_R") is not None else "")
                   + ("" if h.get("enough", True) else " <i>(ma'lumot kam)</i>"))
    if ru:
        out.append(f"🚪 Chiqish: {esc(R.VARIANTS.get(ru['variant'], ru['variant']))}")
    ctx = f"{a['trend'].capitalize()}, RSI {a['rsi']:.0f}"
    if a["setups"]:
        nm = a["setups"][0]["name"]
        ctx += ", " + nm[0].lower() + nm[1:]
    ev = c.get("events") or {}
    if ev.get("earnings"):
        d = datetime.fromisoformat(ev["earnings"])
        ctx += f". Hisobot {uz_date(d)}" if d.date() >= day.date() else f". Oxirgi hisobot {uz_date(d)}"
    out.append(f"<i>{esc(ctx)}</i>")
    for lv, txt in c["risk"]:
        out.append(f"⚠️ <i>{esc(txt)}</i>")
    return out + [""]


def format_text(day, picks, stats):
    f = stats
    when, _ = phase(day)
    short = day.strftime("%Y-%m-%d") in NYSE_EARLY_CLOSE
    lines = ["📡 <b>Kunlik nomzodlar</b>",
             f"<i>{uz_date(day)} {day.year}, {when}" + (". Bugun bozor 13:00 da yopiladi" if short else "") + "</i>"]
    if picks:
        lines.append("<b>" + "   ".join(f"${c['symbol']}" for c in picks) + "</b>")
    lines.append("")
    if f.get("regime"):
        lines += [("🟢 Bozor o'sish rejimida: SPY 200 kunlik o'rtachadan yuqori" if f["regime"] == "SPY>MA200"
                   else "🔴 Bozor pasayish rejimida: SPY 200 kunlik o'rtachadan past, LONG uchun xavfli davr"), ""]
    if not picks:
        if f.get("v2") and not f.get("rules"):
            lines += ["Hozircha birorta qoida 5 yillik sinovning ikkala davridan ham o'tmadi, shuning uchun nomzod "
                      "berilmaydi.", ""]
        else:
            lines += ["Bugun 5 yillik sinovdan o'tgan qoidalarga mos aksiya yo'q. Signal bo'lmagan kuni savdo qilmaslik "
                      "ham strategiya.", ""]
    for k, c in enumerate(picks):
        lines += pick_lines(k, c, day)
    steps = [(f["universe"], "aksiya tekshirildi"), (f["liquid"], "narx $5+ va kuniga $25 mln+ savdo"),
             (f["long"], "Buffet AI LONG reja")]
    if f.get("v2"):
        steps.append((f["passed"], f"5 yillik sinovdan o'tgan {f['rules']} ta qoidadan biriga mos"))
    else:
        steps.append((f["passed"], "Time Machine, risk va 5 yillik tarixdan o'tdi"))
    if f.get("earn"):
        steps.append((f["passed"] - f["earn"], f"hisobot yaqin bo'lgan {f['earn']} tasi chiqarildi"))
    steps.append((len(picks), "nomzod" + (f" (eng yaxshi {TOP_N} tasi)" if f.get("ok", 0) > len(picks) else "")))
    lines += ["<blockquote expandable>🔎 <b>Qanday tanlandi</b>",
              "\n".join(f"{'↓ ' if i else ''}<b>{n}</b> {t}" for i, (n, t) in enumerate(steps)),
              "<i>Qoidalar 2021–24 yillarda topilgan va 2025–26 yillarda alohida tekshirilgan. Natijalar komissiya va "
              "slippage ayirilgan holda. Chiqish (stop va TP) har bir qoidaning o'zida 5 yilda eng yaxshi natija bergan "
              "variant. Shu aksiyaning o'z 5 yillik natijasi manfiy bo'lsa yoki hisoboti 4 hafta ichida bo'lsa, "
              "olinmaydi." + (f" Kalibrlash: {f['cal']}." if f.get("cal") else "") + "</i></blockquote>",
              "💬 Batafsil tahlil: shu yerga <b>$" + (picks[0]["symbol"] if picks else "AAPL") + "</b> deb yozing",
              ""]
    if f.get("partial"):
        lines += [f"<i>ℹ️ {f['partial']}</i>", ""]
    lines.append("<i>Bu qoidalar topgan nomzodlar, sotib olish signali emas. Ehtimollar o'tmishga asoslangan "
                 "simulyatsiya. Investitsion maslahat emas. Buffet AI Warren Buffett bilan bog'liq emas.</i>")
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
    ax.text(0.04, H - 0.55, "Kunlik nomzodlar", fontsize=24, fontweight="bold", color=T["ink"], va="center")
    ax.text(0.04, H - 1.0, f"Buffet AI · {day:%d.%m.%Y} · S&P 500 + NASDAQ (> $2 mlrd) · "
            + phase(day)[0],
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
        ax.text(0.955, top - 0.78, f"stop {mc['p_stop'] * 100:.0f}%", fontsize=9, color=T["sl"], va="center",
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
            ok = 9 * 60 + 50 <= mins <= 10 * 60 + 40            # started at 09:57 New York
        else:
            start = (12 if day in NYSE_EARLY_CLOSE else 15) * 60 + 30   # 30 minutes before the close
            ok = start - 5 <= mins <= start + RUN_WINDOW_MIN      # started at 15:30 (12:30 on short days)
        if not ok:
            print(f"New York {now:%Y-%m-%d %H:%M}: not the {MODE} scan time, nothing to do")
            return
    print(f"New York {now:%Y-%m-%d %H:%M}: scanning")

    stocks = universe()
    prices = download(stocks)
    if phase(now)[1]:                                    # before the open: use only closed daily candles
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

    cal = B.load_calibration()
    approved = cal.get("approved") if cal and cal.get("version", 1) >= 2 else None
    if approved is not None:                             # strict: 150+ trades in the check period too
        approved = [r for r in approved if r["val"]["trades"] >= R.MIN_VAL_TRADES]
    regime = None
    cands, longs = [], 0
    if approved is not None:                             # v2: only rules that passed both test periods
        spy = download(["SPY"], period="2y", min_bars=250).get("SPY")
        spy_ctx = R.spy_context(spy) if spy is not None else {}
        if spy is not None:
            last = spy_ctx[spy.index[-1].date()][0]
            regime = "SPY>MA200" if last else "SPY<MA200"
        for s, df in liquid.items():
            if time.time() - STARTED > TIME_BUDGET * 0.7:
                break
            try:
                is_long, c = evaluate_v2(s, df, stocks[s], approved, spy_ctx, cal)
            except Exception as exc:
                print(f"{s}: {exc}")
                continue
            longs += is_long
            if c:
                cands.append(c)
        print(f"Regime {regime}, approved rules {len(approved)}, LONG plans {longs}, matched rules {len(cands)}")
        cands.sort(key=rank_key_v2, reverse=True)
    else:
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

        for c in cands:
            c["base"] = B.calibration_group(cal, c["a"])
            own = (cal or {}).get("per_stock", {}).get(c["symbol"])
            if own:
                c["history"] = dict(own, fired=own["trades"], enough=own["trades"] >= B.MIN_TRADES,
                                    tested_bars=1260, source="calibration")
        cands.sort(key=rank_key, reverse=True)
        for c in cands[:25]:                                     # no weekly record yet: test the stock's own past now
            if "history" in c or time.time() - STARTED > TIME_BUDGET * 0.85:
                continue
            c["history"] = B.history_test(c["df"], "LONG", B.HORIZON["1D"], deadline=time.time() + 8)
        before = len(cands)
        cands = [c for c in cands
                 if not (c.get("history") and c["history"].get("enough") and (c["history"]["avg_R"] or 0) <= 0.05)
                 and not (c.get("base") and c["base"]["trades"] >= 100 and (c["base"]["avg_R"] or 0) <= 0)]
        print(f"Calibration {'loaded' if cal else 'missing'}: {before - len(cands)} removed by the 5-year records")
        cands.sort(key=rank_key, reverse=True)

    picks, n_earn, n_ok = [], 0, 0
    for c in cands:                                      # all of them are checked, so the count is complete
        if len(picks) >= TOP_N and time.time() - STARTED > TIME_BUDGET * 0.95:
            break
        ev = B.events(c["symbol"], "1D", c["df"])
        if ev.get("earnings_in_window"):
            print(f"{c['symbol']}: earnings {ev['earnings']} inside the plan, skipped")
            n_earn += 1
            continue
        n_ok += 1
        if len(picks) < TOP_N:
            c["events"] = ev
            c["risk"] = [r for r in B.risk_gate(c["a"]["plan"], c["a"], c["df"], ev, "stock") if r[0] == "warn"]
            picks.append(c)

    stats = {"universe": len(stocks), "liquid": len(liquid), "long": longs, "passed": len(cands),
             "earn": n_earn, "ok": n_ok, "v2": approved is not None, "rules": len(approved or []), "regime": regime,
             "cal": uz_date(datetime.fromisoformat(cal["updated"][:10])) if cal else None}
    if len(prices) < len(stocks) * 0.8:
        stats["partial"] = (f"{len(stocks)} aksiyadan {len(prices)} tasining narxi yuklandi "
                            "(qolganlari Yahoo'da topilmadi yoki cheklov).")
    caption = (f"📡 <b>Kunlik nomzodlar</b> · {uz_date(now)}\n"
               + ("  ".join(f"${c['symbol']}" for c in picks) if picks else "Bugun 5 yillik qoidalarga mos nomzod yo'q"))
    send(make_card(now, picks, stats), format_text(now, picks, stats), caption)
    print(f"Sent {len(picks)} candidates in {time.time() - STARTED:.0f}s")


if __name__ == "__main__":
    main()
