"""
Trading Laboratory -> Buffet AI daily scanner ("Kunlik nomzodlar")
Every US trading day at 15:30 New York time (30 minutes before the close) Google Apps Script starts this
script on GitHub Actions.
  1. Universe: S&P 500 members + NASDAQ-listed common stocks with market cap >= $2B (NASDAQ-100 tagged).
  2. Daily bars for all of them (Yahoo Finance, 2 years); price $5+ and 1M+ shares a day.
  3-6. The 26.10 rules (criteria.py): Buffet AI LONG plan, Time Machine, risk check, 250-bar history.
  7. The best 5 without earnings inside the plan are posted to the Buffet AI topic.
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
import criteria as K

# ---------------------------------------------------------------------------
# SETTINGS (you can edit these)
# ---------------------------------------------------------------------------

TOP_N = 5                      # candidates per day
MIN_CAP = 2e9                  # NASDAQ stocks: market cap at least $2B (S&P 500 members always included)
MIN_PRICE = 5.0
MIN_AVG_VOLUME = 1_000_000     # shares per day (20-day average)
RUN_AT_NY = (15, 30)           # 15:30 New York = 30 minutes before the close (12:30 on 13:00 early-close days)
RUN_WINDOW_MIN = 25            # Apps Script may be a little late; never after the close
NYSE_EARLY_CLOSE = {"2026-11-27", "2026-12-24", "2027-11-26"}
TIME_BUDGET = 22 * 60          # seconds for the whole scan
SP500_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
NASDAQ_SCREENER = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&download=true&exchange=nasdaq"
NASDAQ_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
WIKI_SP500 = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
WIKI_NDX = "https://en.wikipedia.org/wiki/Nasdaq-100"

# ---------------------------------------------------------------------------

CHAT_ID = os.environ.get("BUFFET_CHAT_ID", "").strip()
THREAD_ID = os.environ.get("BUFFET_THREAD_ID", "").strip()
FORCE = os.environ.get("FORCE", "").strip().lower() in ("1", "true", "yes")
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
    for line in get(NASDAQ_LISTED).splitlines()[1:]:
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
    """The 26.10 rules 1-3 on one stock -> (said LONG?, candidate or None when a gate fails)."""
    a = B.analyse(df)
    gates = K.plan_gates(a)
    if not gates[0][0]:
        return False, None
    if not all(ok for ok, _ in gates):
        return True, None
    mc = K.time_machine(df, a)
    if not K.tm_ok(mc):
        return True, None
    risk = B.risk_gate(a["plan"], a, df, {}, "stock")
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


OYLAR = ["yanvar", "fevral", "mart", "aprel", "may", "iyun", "iyul", "avgust", "sentyabr", "oktyabr", "noyabr",
         "dekabr"]
MEDALS = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]


def uz_date(d):
    return f"{d.day} {OYLAR[d.month - 1]}"


def num(x):
    """Price without the $ sign, for <code> (tap to copy in Telegram)."""
    return f"{x:,.2f}" if abs(x) >= 1 else f"{x:.4g}"


def pick_lines(k, c, day):
    a, p, mc, info = c["a"], c["a"]["plan"], c["mc"], c["info"]
    t = p["targets"]
    e = p["entry"]
    up = (t[0]["price"] / e - 1) * 100
    tags = ", ".join(info["tags"])
    out = [f"{MEDALS[k]} <b>${esc(c['symbol'])}</b>  {esc(info['name'][:34])} <i>({esc(tags)})</i>"]
    q = [f"▶️ Kirish <code>{num(e)}</code>   🛑 Stop <code>{num(p['stop'])}</code> <i>−{p['risk_pct']}%</i>",
         f"🎯 TP1 <code>{num(t[0]['price'])}</code> <i>+{up:.1f}%, 1:{t[0]['rr']}</i>"
         + (f"   TP2 <code>{num(t[1]['price'])}</code>" if len(t) > 1 else ""),
         f"🎲 20 kunda TP1 birinchi <b>{mc['p_target'] * 100:.0f}%</b>, stop birinchi {mc['p_stop'] * 100:.0f}%"]
    h = c.get("history")
    if h and h["fired"]:
        q.append(f"📜 Oxirgi yil: {h['fired']} signal, {h['target']} TP1 / {h['stop']} stop"
                 + (f", <b>{h['avg_R']:+.2f}R</b>" if h["avg_R"] is not None else ""))
    setups = ", ".join(x["name"] for x in a["setups"])
    q.append(f"📈 {esc(a['trend']).capitalize()}, RSI {a['rsi']:.0f}" + (f". {esc(setups)}" if setups else ""))
    ev = c.get("events") or {}
    if ev.get("earnings"):
        d = datetime.fromisoformat(ev["earnings"])
        q.append(f"📅 Keyingi hisobot {uz_date(d)}" if d.date() >= day.date() else
                 f"📅 Oxirgi hisobot {uz_date(d)}, keyingisi hali e'lon qilinmagan")
    for lv, txt in c["risk"]:
        q.append(f"⚠️ {esc(txt)}")
    return out + ["<blockquote>" + "\n".join(q) + "</blockquote>", ""]


def format_text(day, picks, stats):
    short = day.strftime("%Y-%m-%d") in NYSE_EARLY_CLOSE
    when = "bozor yopilishiga 30 daqiqa qolganda" if not stats.get("as_of") else "o'sha kungi yopilish bo'yicha (backtest)"
    lines = ["📡 <b>Kunlik nomzodlar</b>",
             f"<i>{uz_date(day)} {day.year}, {when}" + (", bugun bozor 13:00 da yopiladi" if short and not stats.get("as_of") else "")
             + "</i>", ""]
    if not picks:
        lines += ["Bugun barcha shartlardan o'tgan aksiya yo'q. Shart bajarilmagan kuni savdo qilmaslik ham strategiya.", ""]
    for k, c in enumerate(picks):
        lines += pick_lines(k, c, day)
    f = stats
    funnel = [f"{f['universe']} aksiya", f"{f['liquid']} likvid", f"{f['long']} LONG reja",
              f"{f['tm']} Time Machine va riskdan o'tdi", f"{f['hist']} oxirgi yil tarixidan o'tdi",
              f"{len(picks)} nomzod"]
    lines += ["<blockquote expandable>🔬 <b>Qanday tanlandi</b>",
              "\n".join(("   " if i else "") + ("↓ " if i else "") + x for i, x in enumerate(funnel)),
              "Qoidalar: Buffet AI LONG reja, TP1 kamida 1:1, RSI 80 dan past, stop 12% ichida. Time Machine'da TP1 "
              "zararsizlik chegarasidan kamida 5 punkt ko'p birinchi keladi. Oxirgi yilda shu aksiyada qoida zarar "
              "qilmagan. Hisoboti 4 hafta ichida bo'lganlar olinmaydi.",
              "</blockquote>",
              "💬 Batafsil tahlil uchun shu yerga yozing: <b>$" + (picks[0]["symbol"] if picks else "AAPL") + "</b>",
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
            + ("yopilishga 30 daqiqa" if not stats.get("as_of") else "backtest"),
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
    as_of = os.environ.get("AS_OF", "").strip()      # backtest: "2026-10-24" = run as if on that day (bars up to it)
    if as_of:
        now = now.replace(year=int(as_of[:4]), month=int(as_of[5:7]), day=int(as_of[8:10]), hour=16, minute=0)
    at = (12, 30) if now.strftime("%Y-%m-%d") in NYSE_EARLY_CLOSE else RUN_AT_NY
    target = now.replace(hour=at[0], minute=at[1], second=0, microsecond=0)
    if not FORCE and not as_of and not (-5 <= (now - target).total_seconds() / 60 <= RUN_WINDOW_MIN
                                        and now.weekday() < 5):
        print(f"New York {now:%Y-%m-%d %H:%M}: not the scan time, nothing to do")
        return
    print(f"New York {now:%Y-%m-%d %H:%M}: scanning")

    stocks = universe()
    spy = download(["SPY"], period="2y", min_bars=250).get("SPY")
    if as_of and spy is not None:
        spy = spy[spy.index.date <= now.date()]
    if spy is not None and not FORCE and not as_of and spy.index[-1].date() != now.date():
        print(f"SPY last bar {spy.index[-1].date()}: market closed today (holiday)")
        return
    prices = download(stocks)
    if as_of:
        prices = {k: v[v.index.date <= now.date()] for k, v in prices.items()}
        prices = {k: v for k, v in prices.items() if len(v) >= 220}
        print(f"Backtest: bars up to {as_of} only")
    if len(prices) < len(stocks) * 0.5:
        B.CHAT_ID, B.THREAD_ID, B.REPLY_TO = CHAT_ID, THREAD_ID, ""
        B.send_text(f"⚠️ Buffet AI kunlik skani: Yahoo Finance'dan ma'lumot yetarli yuklanmadi "
                    f"({len(stocks)} aksiyadan {len(prices)} tasi). Bugungi nomzodlar yuborilmaydi.", reply=False)
        raise SystemExit(f"only {len(prices)} of {len(stocks)} prices")

    liquid = {}
    for s, df in prices.items():
        c, v = df["Close"].to_numpy(dtype=float), df["Volume"].to_numpy(dtype=float)
        if c[-1] >= MIN_PRICE and np.mean(v[-21:-1]) >= MIN_AVG_VOLUME:
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

    # ---- the 26.10 rules (unchanged) ----
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
    n_tm = len(cands)
    print(f"LONG plans: {longs}, passed Time Machine + risk: {n_tm}")

    cands.sort(key=rank_key, reverse=True)
    for c in cands[:25]:                                     # history test for the best ones only
        if time.time() - STARTED > TIME_BUDGET * 0.85:
            break
        c["history"] = K.history_250(c["df"], "LONG", B.HORIZON["1D"], deadline=time.time() + 8)
    cands = [c for c in cands if K.history_ok(c.get("history"))]
    cands.sort(key=rank_key, reverse=True)
    n_hist = len(cands)

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

    stats = {"universe": len(stocks), "liquid": len(liquid), "long": longs, "tm": n_tm, "hist": n_hist,
             "as_of": as_of}
    if len(prices) < len(stocks) * 0.8:
        stats["partial"] = (f"{len(stocks)} aksiyadan {len(prices)} tasining narxi yuklandi "
                            "(qolganlari Yahoo'da topilmadi yoki cheklov).")
    caption = (f"📡 <b>Kunlik nomzodlar</b> · {uz_date(now)}\n"
               + ("  ".join(f"${c['symbol']}" for c in picks) if picks else "Bugun nomzod yo'q"))
    send(make_card(now, picks, stats), format_text(now, picks, stats), caption)
    print(f"Sent {len(picks)} candidates in {time.time() - STARTED:.0f}s")


if __name__ == "__main__":
    main()
