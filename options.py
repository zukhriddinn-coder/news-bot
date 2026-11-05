"""
Trading Laboratory -> options flow for one ticker
A subscriber writes $TICKER in the Options topic. Google Apps Script starts this script (GitHub Actions).
Main source: OptionWhales (Pro API key in the OW_API_KEY secret, data ~15 minutes delayed). The reply has:
  - a GEX chart: dealer gamma by strike, with the price, gamma flip, call wall and put wall,
  - intent flow: what the big option orders are building (bullish / bearish / neutral, confidence, key strikes),
  - premium of large orders: calls bought / sold, puts bought / sold, and the biggest orders (sweep, block ...),
  - GEX levels and gamma regime, max pain,
  - implied volatility rank, open interest change, dark pool blocks, short interest.
If OptionWhales is not available (no key, error), the old Yahoo Finance report is sent instead:
call vs put volume and premium, unusual contracts (volume far above open interest), volume by strike.
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
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import yfinance as yf

# ---------------------------------------------------------------------------
# SETTINGS (you can edit these)
# ---------------------------------------------------------------------------

MAX_DAYS = 90                 # look at expirations up to this many days ahead
MAX_EXPIRIES = 10             # at most this many expirations
TOP_CONTRACTS = 8             # unusual contracts in the answer
MIN_VOLUME = 100              # a contract needs at least this volume to be "unusual"
MIN_VOL_OI = 1.5              # ... and volume at least 1.5x its open interest
MIN_PREMIUM = 25_000          # ... and at least $25K premium traded
CHART_STRIKES = 18            # strikes on the chart
LOCAL_TZ = "Asia/Tashkent"
DISCLAIMER = ("⚠️ Investitsion maslahat emas. Ma'lumot Yahoo Finance'dan, ~15 daqiqa kechikish bilan. "
              "Savdo tomoni (sotib olish yoki sotish) bepul ma'lumotda ko'rinmaydi.")

OW_TOP_ORDERS = 5             # biggest OptionWhales orders in the answer
OW_MIN_CONFIDENCE = 0.5       # an order counts as "bought" / "sold" only when the side is this sure
OW_CHART_STRIKES = 24         # strikes on the GEX chart (nearest to the price)
OW_DISCLAIMER = ("⚠️ Investitsion maslahat emas. Ma'lumot OptionWhales'dan, ~15 daqiqa kechikish bilan. "
                 "Sotib olish / sotish tomoni narx bid yoki ask'ga yaqinligidan aniqlanadi, birja ma'lumoti emas.")
NYSE_HOLIDAYS = {"2026-11-26", "2026-12-25", "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26",
                 "2027-05-31", "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24"}

# ---------------------------------------------------------------------------

OW_KEY = os.environ.get("OW_API_KEY", "").strip()
BOT_TOKEN = re.sub(r"\s+", "", os.environ.get("OPTIONS_BOT_TOKEN", ""))
TICKER_IN = os.environ.get("TICKER", "").strip()
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
    p = {**where(reply), "text": text, "link_preview_options": {"is_disabled": True}}
    if html_mode:
        p["parse_mode"] = "HTML"
    tg("sendMessage", p)


def send_photo(png, caption):
    tg("sendPhoto", {**where(), "caption": caption[:1024], "parse_mode": "HTML"},
       files={"photo": ("options.png", png, "image/png")})


def delete_ack():
    if ACK_ID:
        try:
            tg("deleteMessage", {"chat_id": CHAT_ID, "message_id": int(ACK_ID)})
        except Exception as exc:
            print(f"Could not delete the waiting message: {exc}")


# ----------------------------- Helpers --------------------------------------

def num(v, default=0.0):
    try:
        v = float(v)
        return default if math.isnan(v) or math.isinf(v) else v
    except (TypeError, ValueError):
        return default


def big(v):
    a = abs(v)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            return f"{v / div:,.1f}{suf}"
    return f"{v:,.0f}"


def esc(s):
    return html.escape(str(s), quote=False)


# ----------------------------- Data -----------------------------------------

def collect(symbol):
    t = yf.Ticker(symbol)
    info = t.info or {}
    name = info.get("longName") or info.get("shortName")
    price = num(info.get("currentPrice")) or num(info.get("regularMarketPrice"))
    if not price:
        try:
            price = num(t.fast_info["last_price"])
        except Exception:
            price = 0
    if not name or not price:
        return None
    today = date.today()
    expiries = []
    for e in t.options or ():
        d = date.fromisoformat(e)
        if 0 <= (d - today).days <= MAX_DAYS:
            expiries.append(e)
    expiries = expiries[:MAX_EXPIRIES]
    rows = []
    for e in expiries:
        try:
            chain = t.option_chain(e)
        except Exception as exc:
            print(f"Chain {e}: {exc}")
            continue
        days = (date.fromisoformat(e) - today).days
        for kind, df in (("CALL", chain.calls), ("PUT", chain.puts)):
            for r in df.to_dict("records"):
                vol, oi = num(r.get("volume")), num(r.get("openInterest"))
                bid, ask, last = num(r.get("bid")), num(r.get("ask")), num(r.get("lastPrice"))
                mid = (bid + ask) / 2 if bid > 0 and ask > 0 else last
                rows.append({"kind": kind, "expiry": e, "days": days, "strike": num(r.get("strike")),
                             "vol": vol, "oi": oi, "price": mid, "iv": num(r.get("impliedVolatility")),
                             "premium": vol * mid * 100})
    return {"symbol": symbol, "name": name, "price": price, "expiries": expiries, "rows": rows}


# ----------------------------- Analysis -------------------------------------

def analyse(d):
    rows, price = d["rows"], d["price"]
    calls = [r for r in rows if r["kind"] == "CALL"]
    puts = [r for r in rows if r["kind"] == "PUT"]
    a = {
        "call_vol": sum(r["vol"] for r in calls), "put_vol": sum(r["vol"] for r in puts),
        "call_prem": sum(r["premium"] for r in calls), "put_prem": sum(r["premium"] for r in puts),
        "call_oi": sum(r["oi"] for r in calls), "put_oi": sum(r["oi"] for r in puts),
    }
    a["pc_vol"] = a["put_vol"] / a["call_vol"] if a["call_vol"] else None
    a["pc_oi"] = a["put_oi"] / a["call_oi"] if a["call_oi"] else None
    total_prem = a["call_prem"] + a["put_prem"]
    a["call_share"] = a["call_prem"] / total_prem if total_prem else None

    # nearest expiry: ATM implied volatility, expected move, max pain
    if d["expiries"]:
        e = d["expiries"][0]
        near = [r for r in rows if r["expiry"] == e]
        a["near_expiry"], a["near_days"] = e, max(near[0]["days"], 1) if near else 1
        atm = sorted(near, key=lambda r: abs(r["strike"] - price))[:4]
        ivs = [r["iv"] for r in atm if r["iv"] > 0.01]
        if ivs:
            a["iv"] = sum(ivs) / len(ivs)
            a["move"] = price * a["iv"] * math.sqrt(a["near_days"] / 365)
        strikes = sorted({r["strike"] for r in near})
        if strikes and sum(r["oi"] for r in near):
            def pain(s):
                return sum(r["oi"] * max(0, s - r["strike"]) for r in near if r["kind"] == "CALL") + \
                       sum(r["oi"] * max(0, r["strike"] - s) for r in near if r["kind"] == "PUT")
            a["max_pain"] = min(strikes, key=pain)

    unusual = [r for r in rows
               if r["vol"] >= MIN_VOLUME and r["premium"] >= MIN_PREMIUM
               and (r["vol"] >= MIN_VOL_OI * r["oi"] or (r["oi"] == 0 and r["vol"] >= 2 * MIN_VOLUME))]
    a["unusual"] = sorted(unusual, key=lambda r: r["premium"], reverse=True)[:TOP_CONTRACTS]

    by_strike = {}
    for r in rows:
        s = by_strike.setdefault(r["strike"], {"CALL": 0.0, "PUT": 0.0})
        s[r["kind"]] += r["vol"]
    top = sorted(by_strike.items(), key=lambda kv: kv[1]["CALL"] + kv[1]["PUT"], reverse=True)[:CHART_STRIKES]
    a["by_strike"] = sorted(top)
    return a


def mood(share):
    if share is None:
        return "—", "neutral"
    if share >= 0.65:
        return "Bullish 🟢 (call'larga ko'proq pul)", "up"
    if share <= 0.35:
        return "Bearish 🔴 (put'larga ko'proq pul)", "down"
    return "Neytral 🟡 (aralash)", "neutral"


# ----------------------------- Text -----------------------------------------

def short_date(e):
    return datetime.fromisoformat(e).strftime("%d.%m.%y")


def format_text(d, a):
    price = d["price"]
    now = datetime.now(ZoneInfo(LOCAL_TZ)).strftime("%d.%m.%Y %H:%M")
    word, _ = mood(a["call_share"])
    lines = [f"📊 <b>${esc(d['symbol'])} — optsion faolligi</b>",
             f"<i>{esc(d['name'])} · joriy narx ${price:,.2f} · {now}</i>", "",
             f"⚖️ <b>Umumiy oqim</b> <i>(keyingi {MAX_DAYS} kundagi muddatlar)</i>",
             f"   Call hajmi: <b>{big(a['call_vol'])}</b> · Put hajmi: <b>{big(a['put_vol'])}</b>"
             + (f" · P/C: {a['pc_vol']:.2f}" if a["pc_vol"] is not None else ""),
             f"   Call premium: <b>${big(a['call_prem'])}</b>"
             + (f" ({a['call_share'] * 100:.0f}%)" if a["call_share"] is not None else "")
             + f" · Put premium: <b>${big(a['put_prem'])}</b>",
             f"   Ochiq pozitsiyalar P/C: {a['pc_oi']:.2f}" if a["pc_oi"] is not None else "",
             f"   Kayfiyat: <b>{word}</b>"]
    if a.get("iv"):
        pct = a["move"] / price * 100
        lines.append(f"   ATM IV ({short_date(a['near_expiry'])}): {a['iv'] * 100:.0f}% · "
                     f"kutilayotgan harakat ±${a['move']:,.2f} (±{pct:.1f}%)")
    if a.get("max_pain"):
        lines.append(f"   Max pain ({short_date(a['near_expiry'])}): ${a['max_pain']:,.2f}")
    lines.append("")

    if a["unusual"]:
        lines.append("🔥 <b>G'ayrioddiy kontraktlar</b> <i>(hajm ochiq pozitsiyalardan ancha ko'p)</i>")
        for i, r in enumerate(a["unusual"], 1):
            icon = "🟢" if r["kind"] == "CALL" else "🔴"
            otm = (r["strike"] / price - 1) * 100
            ratio = f"{r['vol'] / r['oi']:.1f}x" if r["oi"] else "yangi"
            lines.append(f"{i}. {icon} <b>{r['kind']} ${r['strike']:,.2f}</b> · {short_date(r['expiry'])} "
                         f"({r['days']} kun)")
            lines.append(f"    Hajm {big(r['vol'])} · OI {big(r['oi'])} · Vol/OI {ratio}")
            lines.append(f"    Premium ≈ <b>${big(r['premium'])}</b> · IV {r['iv'] * 100:.0f}% · "
                         f"strike narxdan {otm:+.1f}%")
            lines.append("")
    else:
        lines += ["🔥 <i>Bugun g'ayrioddiy optsion faolligi topilmadi.</i>", ""]

    lines.append(f"<i>{esc(DISCLAIMER)}</i>")
    return "\n".join(x for x in lines if x is not None).replace("\n\n\n", "\n\n").strip()


# ----------------------------- Chart ----------------------------------------

THEME = {"bg": "#0b1220", "panel": "#111a2e", "line": "#1e2a44", "ink": "#e6edf7", "muted": "#8a99b8",
         "up": "#22c55e", "down": "#ef4444", "neutral": "#eab308", "accent": "#f5b301"}


def _font():
    """Inter from the repo's fonts/ folder (fast), else an installed Inter, else DejaVu Sans."""
    import glob
    from matplotlib import font_manager
    for path in glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", "*.[ot]tf")):
        try:
            font_manager.fontManager.addfont(path)
        except Exception as exc:
            print(f"Font {path}: {exc}")
    names = {f.name for f in font_manager.fontManager.ttflist}
    return "Inter" if "Inter" in names else "DejaVu Sans"


def make_chart(d, a):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    T = THEME
    plt.rcParams["font.family"] = _font()
    strikes = a["by_strike"]
    if not strikes:
        return None
    n = len(strikes)
    fig_h = 3.9 + 0.36 * n
    fig = plt.figure(figsize=(10, fig_h), dpi=160, facecolor=T["bg"])

    head = fig.add_axes([0, 1 - 2.2 / fig_h, 1, 2.2 / fig_h])
    head.set_axis_off()
    head.set_xlim(0, 1)
    head.set_ylim(0, 1)
    head.text(0.04, 0.78, f"${d['symbol']}", fontsize=28, fontweight="bold", color=T["ink"], va="center")
    head.text(0.04, 0.51, d["name"][:44], fontsize=12.5, color=T["ink"], va="center")
    head.text(0.04, 0.30, f"Optsion faolligi · {datetime.now(ZoneInfo(LOCAL_TZ)):%d.%m.%Y %H:%M}",
              fontsize=9.5, color=T["muted"], va="center")

    def stat(x, label, value, color):
        head.text(x, 0.80, label, fontsize=9.5, color=T["muted"], va="center")
        head.text(x, 0.56, value, fontsize=19, fontweight="bold", color=color, va="center")

    stat(0.52, "Joriy narx", f"${d['price']:,.2f}", T["ink"])
    if a["call_share"] is not None:
        stat(0.74, "Call / Put premium", f"{a['call_share'] * 100:.0f}% / {(1 - a['call_share']) * 100:.0f}%",
             T["ink"])
        word, tone = mood(a["call_share"])
        color = T[tone]
        label = word.split(" ")[0].upper()
        head.text(0.74, 0.25, f" {label} ", fontsize=11, fontweight="bold", color=color, va="center",
                  bbox=dict(boxstyle="round,pad=0.45,rounding_size=0.8", facecolor=T["bg"],
                            edgecolor=color, linewidth=1.3))
    head.plot([0.04, 0.96], [0.04, 0.04], color=T["line"], linewidth=1)

    ax = fig.add_axes([0.16, 0.95 / fig_h, 0.78, (fig_h - 3.25) / fig_h], facecolor=T["bg"])
    ys = list(range(n))
    calls = [s[1]["CALL"] for s in strikes]
    puts = [-s[1]["PUT"] for s in strikes]
    ax.barh(ys, calls, color=T["up"], height=0.7, zorder=2, label="Call hajmi")
    ax.barh(ys, puts, color=T["down"], height=0.7, zorder=2, label="Put hajmi")
    ax.axvline(0, color=T["line"], linewidth=1, zorder=1)
    ax.set_yticks(ys)
    ax.set_yticklabels([f"${s[0]:,.2f}".rstrip("0").rstrip(".") for s in strikes], fontsize=9, color=T["ink"])
    ax.tick_params(axis="y", length=0, pad=6)
    ax.tick_params(axis="x", colors=T["muted"], labelsize=8.5, length=0)
    ax.xaxis.set_major_formatter(lambda v, _: big(abs(v)))
    span = max([abs(v) for v in calls + puts] + [1])
    ax.set_xlim(-span * 1.15, span * 1.15)
    ax.grid(axis="x", color=T["line"], linewidth=0.8, zorder=0)
    for side in ax.spines.values():
        side.set_visible(False)

    # current price between the strikes around it
    price = d["price"]
    below = [i for i, s in enumerate(strikes) if s[0] <= price]
    if below and below[-1] < n - 1:
        i = below[-1]
        lo, hi = strikes[i][0], strikes[i + 1][0]
        y = i + (price - lo) / (hi - lo) if hi > lo else i
        ax.axhline(y, color=T["accent"], linestyle=(0, (4, 3)), linewidth=1.3, zorder=3)
        ax.text(-span * 1.13, y, f" narx ${price:,.2f} ", color=T["accent"], fontsize=8.5, ha="left",
                va="center", fontweight="bold", zorder=5,
                bbox=dict(boxstyle="round,pad=0.25,rounding_size=0.5", facecolor=T["bg"],
                          edgecolor=T["accent"], linewidth=1))
    ax.text(-span * 1.13, n - 0.4, "◀ PUT hajmi", color=T["down"], fontsize=9.5, fontweight="bold", va="bottom")
    ax.text(span * 1.13, n - 0.4, "CALL hajmi ▶", color=T["up"], fontsize=9.5, fontweight="bold",
            va="bottom", ha="right")

    fig.text(0.04, 0.32 / fig_h, f"Strike bo'yicha bugungi savdo hajmi · keyingi {MAX_DAYS} kundagi muddatlar",
             fontsize=8, color=T["muted"])
    fig.text(0.96, 0.32 / fig_h, "Trading Laboratory · Yahoo Finance (~15 daq.)", fontsize=8,
             color=T["muted"], ha="right")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=T["bg"])
    plt.close(fig)
    return buf.getvalue()


# ----------------------------- OptionWhales ---------------------------------

OW_BASE = "https://api.optionwhales.io/v1"
OW_SENT = False


def ow_get(path, **params):
    """One OptionWhales call. Returns the JSON or None. The key is never printed."""
    url = OW_BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    for k in range(3):
        req = urllib.request.Request(url, headers={"X-API-Key": OW_KEY, "Accept": "application/json",
                                                   "User-Agent": "TradingLab/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.loads(r.read().decode("utf-8") or "null")
        except urllib.error.HTTPError as e:
            if e.code == 429 and k < 2:
                time.sleep(num(e.headers.get("Retry-After"), 5) + 1)
                continue
            print(f"OptionWhales {path}: HTTP {e.code} {e.read().decode('utf-8', 'replace')[:120]}")
            return None
        except Exception as e:
            print(f"OptionWhales {path}: {type(e).__name__}: {e}")
            return None
    return None


def ny_now():
    """New York time without extra packages (US DST: 2nd Sunday of March -> 1st Sunday of November)."""
    utc = datetime.now(timezone.utc)

    def nth_sunday(y, m, n):
        d = datetime(y, m, 1, tzinfo=timezone.utc)
        d += timedelta(days=(6 - d.weekday()) % 7)
        return d + timedelta(weeks=n - 1)

    start = nth_sunday(utc.year, 3, 2).replace(hour=7)
    end = nth_sunday(utc.year, 11, 1).replace(hour=6)
    return (utc - timedelta(hours=4 if start <= utc < end else 5)).replace(tzinfo=None)


def trading_day(d):
    return d.weekday() < 5 and d.isoformat() not in NYSE_HOLIDAYS


def sessions():
    """(main session, previous session): today once the market has opened, else the last trading day."""
    now = ny_now()
    d = now.date()
    if not (trading_day(d) and now.hour * 60 + now.minute >= 9 * 60 + 30):
        d -= timedelta(days=1)
        while not trading_day(d):
            d -= timedelta(days=1)
    p = d - timedelta(days=1)
    while not trading_day(p):
        p -= timedelta(days=1)
    return d.isoformat(), p.isoformat()


def ow_collect(symbol):
    """Everything OptionWhales knows about one ticker (7 calls, in parallel). None if nothing came back."""
    s0, s1 = sessions()

    def flow():
        for s in (s0, s1):
            j = ow_get(f"/flow/{s}/{symbol}")
            if j and j.get("ranking"):
                return {"session": s, "ranking": j["ranking"], "final": s != s0 or not j.get("is_live", False)}
        return None

    def gex():
        for s in (s0, s1):
            j = ow_get(f"/gex/{s}/{symbol}")
            if j and (j.get("summary") or {}).get("key_levels"):
                return {"session": s, **j}
        return None

    def tape():
        j = ow_get("/options-flow/tape", ticker=symbol, limit=1500)          # the latest session (as in the probe)
        if j and j.get("orders"):
            return {**j, "session": j.get("session") or s0}
        j = ow_get("/options-flow/tape", ticker=symbol, session=s1, limit=1500)
        if j and j.get("orders"):
            return {**j, "session": j.get("session") or s1}
        return None

    jobs = {"flow": flow, "gex": gex, "tape": tape,
            "iv": lambda: ow_get(f"/volatility/{symbol}/iv-rank"),
            "oi": lambda: ow_get(f"/oi/{symbol}/change"),
            "dark": lambda: ow_get("/darkpool/recent", ticker=symbol, limit=20),
            "short": lambda: ow_get(f"/short/{symbol}/interest")}
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {k: ex.submit(f) for k, f in jobs.items()}
        out = {k: f.result() for k, f in futures.items()}
    out["session"] = s0
    if not (out["flow"] or out["gex"] or out["tape"]):
        return None
    return out


# --- reading the answers -----------------------------------------------------

BIAS = {"bullish": ("🟢", "Bullish", "o'sish tomonga", "up"), "bearish": ("🔴", "Bearish", "pasayish tomonga", "down"),
        "neutral": ("🟡", "Neytral", "aniq yo'nalishsiz", "neutral")}
INTENT_PRIMARY = {"gamma": "gamma pozitsiyalash", "longvol": "volatillik xaridi (katta harakat kutilmoqda)",
                  "shortvol": "volatillik sotuvi (tinch bozor kutilmoqda)", "directional": "yo'nalishga tikish",
                  "hedge": "himoya (hedge)", "income": "premium yig'ish (daromad strategiyasi)"}
INTENT_MODS = {"pin-like": "narx yirik strike atrofida ushlanib qolishi mumkin (pin)",
               "position-build": "yangi pozitsiyalar qurilmoqda",
               "position-unwind": "eski pozitsiyalar yopilmoqda",
               "bullish-convex": "o'sishdan ko'proq foyda oladigan pozitsiyalar",
               "bearish-convex": "pasayishdan ko'proq foyda oladigan pozitsiyalar",
               "low-coherence": "oqim tarqoq, buyurtmalar turli tomonga",
               "event": "voqea (hisobot, yangilik) kutilmoqda"}


def side_of(o):
    """'buy' / 'sell' / None (side not sure enough)."""
    d = str(o.get("direction") or "").lower()
    if d not in ("buy", "sell") or num(o.get("direction_confidence")) < OW_MIN_CONFIDENCE:
        return None
    return d


def is_call(o):
    return str(o.get("right") or o.get("opt_type") or "").upper().startswith("C")


def premium_split(orders):
    agg = {"call_buy": 0.0, "call_sell": 0.0, "put_buy": 0.0, "put_sell": 0.0, "unknown": 0.0}
    for o in orders:
        sd = side_of(o)
        prem = num(o.get("premium"))
        if sd is None:
            agg["unknown"] += prem
        else:
            agg[("call_" if is_call(o) else "put_") + sd] += prem
    bull = agg["call_buy"] + agg["put_sell"]           # bets on a rise
    bear = agg["put_buy"] + agg["call_sell"]           # bets on a fall
    agg["bull"], agg["bear"] = bull, bear
    agg["bull_share"] = bull / (bull + bear) if bull + bear else None
    return agg


def flip_level(kl, spot):
    """The global gamma flip, or the local transition when the global one is far from the price."""
    g, l = num(kl.get("global_gamma_flip") or kl.get("gamma_flip"), None), num(kl.get("local_gamma_transition"), None)
    if g and spot and abs(g / spot - 1) <= 0.2:
        return g
    return l or g


def wall(kl, key):
    w = kl.get(key) or {}
    return num(w.get("strike"), None) if isinstance(w, dict) else None


def uz_session(s):
    d = date.fromisoformat(s)
    return f"{d.day} {['yanvar', 'fevral', 'mart', 'aprel', 'may', 'iyun', 'iyul', 'avgust', 'sentyabr', 'oktyabr', 'noyabr', 'dekabr'][d.month - 1]}"


def money(v):
    return ("−" if v < 0 else "") + "$" + big(abs(v))


def price_s(v):
    return re.sub(r"\.00$", "", f"${v:,.2f}") if v is not None else "—"


def local_hm(ts):
    """UTC time from OptionWhales -> HH:MM in Tashkent."""
    try:
        t = datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", str(ts).replace("Z", "+00:00")))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t.astimezone(ZoneInfo(LOCAL_TZ)).strftime("%H:%M")
    except Exception:
        return ""


def sgn_pct(v):
    v = num(v)
    return f"{'+' if v >= 0 else '−'}{abs(v):.2f}%"


def ow_analyse(symbol, ow):
    a = {"symbol": symbol, "session": ow.get("session")}
    g = ow.get("gex") or {}
    sm = g.get("summary") or {}
    kl = sm.get("key_levels") or {}
    spot = num(sm.get("spot"), None)
    tp = ow.get("tape") or {}
    orders = tp.get("orders") or []
    if not spot and orders:
        spot = num(orders[0].get("spot_price"), None)
    a["spot"] = spot
    if kl:
        a["gex"] = {"session": g["session"], "regime": str(kl.get("gamma_regime") or "").lower(),
                    "flip": flip_level(kl, spot), "call_wall": wall(kl, "call_wall"), "put_wall": wall(kl, "put_wall"),
                    "max_pain": num(kl.get("max_pain"), None), "max_pain_expiry": kl.get("max_pain_expiry"),
                    "pc_oi": num(sm.get("put_call_oi_ratio"), None), "net_gex": num(sm.get("net_gex"), None),
                    "by_strike": g.get("by_strike") or []}
    fl = ow.get("flow")
    if fl:
        r = fl["ranking"]
        mods = [m.strip() for m in str(r.get("intent_modifiers") or "").split(",") if m.strip()]
        a["flow"] = {"session": fl["session"], "bias": str(r.get("direction_bias") or "neutral").lower(),
                     "confidence": num(r.get("intent_confidence"), None), "label": r.get("intent_label") or "",
                     "primary": str(r.get("intent_primary") or ""), "mods": mods,
                     "strikes": [x for x in str(r.get("key_strikes") or "").split(";") if x][:5],
                     "long_dated": bool(r.get("long_dated_bias")), "short_dom": num(r.get("short_term_dominance")),
                     "build": num(r.get("thesis_build_score"))}
    if orders:
        a["tape"] = {"session": tp["session"], "count": len(orders), "matched": int(num(tp.get("matched"), len(orders))),
                     "split": premium_split(orders),
                     "top": sorted(orders, key=lambda o: -num(o.get("premium")))[:OW_TOP_ORDERS],
                     "delay": num((tp.get("data") or {}).get("delay_seconds"), 900)}
    iv = ow.get("iv") or {}
    if iv.get("iv_rank") is not None:
        a["iv"] = {"current": num(iv.get("current")), "rank": num(iv.get("iv_rank")),
                   "pct": num(iv.get("iv_percentile"))}
    oi = ow.get("oi") or {}
    if oi.get("change"):
        a["oi"] = {"change": oi["change"], "pct": oi.get("pct_change") or {}, "latest": oi.get("latest") or {}}
    dk = ow.get("dark") or {}
    prints = dk.get("prints") or []
    if prints:
        a["dark"] = sorted(prints, key=lambda p: -num(p.get("premium")))[:3]
    sh = ow.get("short") or {}
    recs = sh.get("records") or []
    if recs:
        a["short"] = recs[0]
    return a


# --- text ----------------------------------------------------------------------

def flow_mood(a):
    """The headline colour: intent flow first, else the premium balance of large orders."""
    f = a.get("flow")
    if f:
        return BIAS.get(f["bias"], BIAS["neutral"])
    t = a.get("tape")
    share = t["split"]["bull_share"] if t else None
    if share is None:
        return BIAS["neutral"]
    return BIAS["bullish"] if share >= 0.6 else BIAS["bearish"] if share <= 0.4 else BIAS["neutral"]


def ow_text(name, a):
    now = datetime.now(ZoneInfo(LOCAL_TZ)).strftime("%d.%m.%Y %H:%M")
    spot = a.get("spot")
    lines = [f"🐋 <b>${esc(a['symbol'])} — optsion oqimi</b>", "",
             f"<i>{esc(name)} · narx {price_s(spot)} · {now} (Toshkent)</i>", ""]

    f = a.get("flow")
    lines.append("🧭 <b>Yirik o'yinchilar niyati</b>")
    lines.append("")
    if f:
        icon, word, hint, _ = BIAS.get(f["bias"], BIAS["neutral"])
        conf = f" · ishonch {f['confidence'] * 100:.0f}%" if f["confidence"] is not None else ""
        lines.append(f"{icon} <b>{word}</b>: {hint}{conf}")
        what = INTENT_PRIMARY.get(f["primary"].lower().replace(" ", ""), "")
        if what:
            lines.append(f"Asosiy strategiya: {what}")
        elif f["label"]:
            lines.append(f"Asosiy strategiya: <i>{esc(f['label'])}</i>")
        print(f"intent: primary={f['primary']!r} label={f['label']!r} mods={f['mods']}")   # to extend the lists
        mods = [INTENT_MODS.get(m, m) for m in f["mods"]]
        if mods:
            lines.append("Belgilar: " + "; ".join(mods))
        horizon = ("asosan uzoq muddatli kontraktlar" if f["long_dated"] else
                   "asosan qisqa muddatli kontraktlar" if f["short_dom"] >= 0.5 else "turli muddatlar")
        lines.append(f"Muddat: {horizon}")
        if f["strikes"]:
            lines.append("Asosiy strike'lar: " + ", ".join(price_s(num(x)) for x in f["strikes"]))
        if f["session"] != a.get("session"):
            lines.append(f"<i>{uz_session(f['session'])} sessiyasi bo'yicha</i>")
    else:
        lines.append("<i>Bu tiker bo'yicha oqim niyati hisoblanmagan (yirik buyurtmalar kam).</i>")
    lines.append("")

    t = a.get("tape")
    if t:
        sp = t["split"]
        lines.append(f"💰 <b>Yirik buyurtmalar premiumi</b>  <i>{uz_session(t['session'])}, 100+ kontrakt</i>")
        lines.append("")
        lines.append(f"🟢 Call sotib olindi <b>{money(sp['call_buy'])}</b> · 🔴 Call sotildi <b>{money(sp['call_sell'])}</b>")
        lines.append(f"🔴 Put sotib olindi <b>{money(sp['put_buy'])}</b> · 🟢 Put sotildi <b>{money(sp['put_sell'])}</b>")
        if sp["unknown"]:
            lines.append(f"🟡 Tomoni aniq emas: {money(sp['unknown'])}")
        if sp["bull_share"] is not None:
            sh = sp["bull_share"] * 100
            icon = "🟢" if sh >= 60 else "🔴" if sh <= 40 else "🟡"
            lines.append(f"{icon} O'sishga tikilgan pul <b>{sh:.0f}%</b>, pasayishga <b>{100 - sh:.0f}%</b>")
        lines.append("")
        lines.append(f"🐳 <b>Eng yirik buyurtmalar</b>  <i>{t['matched']} tadan top {len(t['top'])}, Toshkent vaqti</i>")
        lines.append("")
        for i, o in enumerate(t["top"], 1):
            sd = side_of(o)
            c = is_call(o)
            bull = (c and sd == "buy") or (not c and sd == "sell")
            icon = "🟡" if sd is None else "🟢" if bull else "🔴"
            word = "sotib olindi" if sd == "buy" else "sotildi" if sd == "sell" else "tomoni aniq emas"
            kind = str(o.get("order_type") or "").upper()
            exp = str(o.get("expiration") or "")[:10]
            dte = int(num(o.get("dte")))
            lines.append(f"{i}. {icon} <b>{'CALL' if c else 'PUT'} {price_s(num(o.get('strike')))}</b> · "
                         f"{short_date(exp) if exp else '—'} ({dte} kun) · {esc(kind)} · {word}")
            iv_ = num(o.get("implied_volatility"))
            lines.append(f"    {int(num(o.get('contracts'))):,} kontrakt · premium <b>{money(num(o.get('premium')))}</b>"
                         + (f" · IV {iv_ * 100:.0f}%" if iv_ else "")
                         + (f" · {local_hm(o.get('timestamp'))}" if local_hm(o.get("timestamp")) else ""))
        lines.append("")

    gx = a.get("gex")
    if gx:
        lines.append(f"🧲 <b>GEX darajalari</b>  <i>dilerlar gammasi, {uz_session(gx['session'])}</i>")
        lines.append("")
        if gx["regime"] == "short":
            lines.append("🔴 Gamma rejimi: <b>short</b>, dilerlar harakatni kuchaytiradi, keskin tebranish ehtimoli yuqori")
        elif gx["regime"] == "long":
            lines.append("🟢 Gamma rejimi: <b>long</b>, dilerlar harakatni so'ndiradi, narx barqarorroq")
        lvl = []
        if gx["call_wall"]:
            lvl.append(f"Call wall <b>{price_s(gx['call_wall'])}</b> (qarshilik)")
        if gx["put_wall"]:
            lvl.append(f"Put wall <b>{price_s(gx['put_wall'])}</b> (qo'llab-quvvatlash)")
        if lvl:
            lines.append(" · ".join(lvl))
        if gx["flip"]:
            side = ""
            if spot:
                side = " · narx undan yuqorida" if spot > gx["flip"] else " · narx undan pastda"
            lines.append(f"Gamma flip <b>{price_s(gx['flip'])}</b>{side}")
        if gx["max_pain"]:
            exp = f" ({short_date(gx['max_pain_expiry'])})" if gx.get("max_pain_expiry") else ""
            lines.append(f"Max pain <b>{price_s(gx['max_pain'])}</b>{exp}")
        if gx["pc_oi"]:
            lines.append(f"Put/Call OI nisbati {gx['pc_oi']:.2f}")
        lines.append("")

    iv = a.get("iv")
    if iv:
        r = iv["rank"] * 100
        icon, word = (("🔴", "yuqori: optsionlar qimmat") if r >= 70 else ("🟢", "past: optsionlar arzon") if r <= 30
                      else ("🟡", "o'rtacha"))
        lines += ["📈 <b>Implied volatility</b>", "",
                  f"{icon} IV {iv['current'] * 100:.0f}% · IV rank <b>{r:.0f}%</b> · percentile {iv['pct'] * 100:.0f}%, {word}", ""]

    oi = a.get("oi")
    if oi:
        ch, pc = oi["change"], oi["pct"]

        def oi_s(k):
            v = num(ch.get(k))
            p = pc.get(k)
            return (f"{'+' if v >= 0 else '−'}{big(abs(v))}" + (f" ({sgn_pct(p)})" if p is not None else ""))
        lines += ["📊 <b>Ochiq pozitsiyalar o'zgarishi</b>  <i>bir kunda</i>", "",
                  f"{'🟢' if num(ch.get('call_oi')) >= 0 else '🔴'} Call OI {oi_s('call_oi')} · "
                  f"{'🔴' if num(ch.get('put_oi')) >= 0 else '🟢'} Put OI {oi_s('put_oi')}", ""]

    dk = a.get("dark")
    if dk:
        lines += ["🌑 <b>Dark pool</b>  <i>eng yirik bloklar, Toshkent vaqti</i>", ""]
        for p in dk:
            lab = str(p.get("nbbo_label") or "")
            icon, word = (("🟢", "ask'da: xaridor faol") if "ask" in lab else ("🔴", "bid'da: sotuvchi faol") if "bid" in lab
                          else ("🟡", "o'rtada"))
            hm = local_hm(p.get("executed_at")) or (str(p.get("executed_at_et") or "")[:5] + " ET")
            lines.append(f"{icon} {hm} · {int(num(p.get('size'))):,} dona · "
                         f"{price_s(num(p.get('price')))} · <b>{money(num(p.get('premium')))}</b> · {word}")
        lines.append("")

    sh = a.get("short")
    if sh:
        lines += [f"🩳 <b>Short interest</b>: {big(num(sh.get('short_interest')))} aksiya · days to cover "
                  f"{num(sh.get('days_to_cover')):.1f} <i>({short_date(sh['settlement_date']) if sh.get('settlement_date') else '—'} holatiga)</i>", ""]

    lines.append(f"<i>{esc(OW_DISCLAIMER)}</i>")
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def split_text(text, limit=3900):
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


# --- GEX chart -----------------------------------------------------------------

def gex_chart(name, a):
    gx = a.get("gex")
    spot = a.get("spot")
    if not gx or not spot:
        return None
    near = [r for r in gx["by_strike"] if num(r.get("strike")) > 0 and abs(num(r.get("strike")) / spot - 1) <= 0.15]
    must = {x for x in (gx["call_wall"], gx["put_wall"]) if x}
    keep = [r for r in near if num(r["strike"]) in must]
    rest = sorted((r for r in near if num(r["strike"]) not in must and abs(num(r.get("net_gex"))) > 0),
                  key=lambda r: -abs(num(r.get("net_gex"))))       # strongest gamma levels first
    top_abs = abs(num(rest[0].get("net_gex"))) if rest else 0
    rest = [r for r in rest if abs(num(r.get("net_gex"))) >= 0.03 * top_abs]     # drop almost empty strikes
    rows = sorted(keep + rest[:max(0, OW_CHART_STRIKES - len(keep))], key=lambda r: num(r["strike"]))
    if len(rows) < 4:
        return None
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    T = THEME
    plt.rcParams["font.family"] = _font()
    plt.rcParams["text.parse_math"] = False
    n = len(rows)
    fig_h = 3.9 + 0.34 * n
    fig = plt.figure(figsize=(10, fig_h), dpi=160, facecolor=T["bg"])
    head = fig.add_axes([0, 1 - 2.2 / fig_h, 1, 2.2 / fig_h])
    head.set_axis_off()
    head.set_xlim(0, 1)
    head.set_ylim(0, 1)
    head.text(0.04, 0.78, f"${a['symbol']}", fontsize=28, fontweight="bold", color=T["ink"], va="center")
    head.text(0.04, 0.51, name[:44], fontsize=12.5, color=T["ink"], va="center")
    head.text(0.04, 0.30, f"Optsion oqimi · GEX · {uz_session(gx['session'])} · "
                          f"{datetime.now(ZoneInfo(LOCAL_TZ)):%d.%m.%Y %H:%M}", fontsize=9.5, color=T["muted"],
              va="center")

    def stat(x, label, value, color):
        head.text(x, 0.80, label, fontsize=9.5, color=T["muted"], va="center")
        head.text(x, 0.56, value, fontsize=19, fontweight="bold", color=color, va="center")

    stat(0.52, "Narx", price_s(spot), T["ink"])
    reg = gx["regime"]
    stat(0.74, "Gamma rejimi", reg.upper() if reg else "—", T["down"] if reg == "short" else T["up"] if reg == "long"
         else T["ink"])
    icon, word, _, tone = flow_mood(a)
    color = T[tone]
    head.text(0.52, 0.25, f" Oqim: {word.upper()} ", fontsize=11, fontweight="bold", color=color, va="center",
              bbox=dict(boxstyle="round,pad=0.45,rounding_size=0.8", facecolor=T["bg"], edgecolor=color,
                        linewidth=1.3))
    head.plot([0.04, 0.96], [0.04, 0.04], color=T["line"], linewidth=1)

    ax = fig.add_axes([0.2, 0.95 / fig_h, 0.74, (fig_h - 3.25) / fig_h], facecolor=T["bg"])
    ys = list(range(n))
    vals = [num(r.get("net_gex")) for r in rows]
    ax.barh(ys, vals, color=[T["up"] if v >= 0 else T["down"] for v in vals], height=0.7, zorder=2)
    ax.axvline(0, color=T["line"], linewidth=1, zorder=1)
    strikes = [num(r["strike"]) for r in rows]
    labels = []
    for s_ in strikes:
        tag = ""
        if gx["call_wall"] and abs(s_ - gx["call_wall"]) < 1e-6:
            tag = "call wall  "
        elif gx["put_wall"] and abs(s_ - gx["put_wall"]) < 1e-6:
            tag = "put wall  "
        labels.append(f"{tag}{price_s(s_)}")
    ax.set_yticks(ys)
    ax.set_yticklabels(labels, fontsize=9, color=T["ink"])
    for lab, s_ in zip(ax.get_yticklabels(), strikes):
        if gx["call_wall"] and abs(s_ - gx["call_wall"]) < 1e-6:
            lab.set_color(T["up"]); lab.set_fontweight("bold")
        elif gx["put_wall"] and abs(s_ - gx["put_wall"]) < 1e-6:
            lab.set_color(T["down"]); lab.set_fontweight("bold")
    ax.tick_params(axis="y", length=0, pad=6)
    ax.tick_params(axis="x", colors=T["muted"], labelsize=8.5, length=0)
    ax.xaxis.set_major_formatter(lambda v, _: ("−" if v < 0 else "") + big(abs(v)))
    span = max([abs(v) for v in vals] + [1])
    ax.set_xlim(-span * 1.15, span * 1.15)
    ax.grid(axis="x", color=T["line"], linewidth=0.8, zorder=0)
    for side in ax.spines.values():
        side.set_visible(False)

    def y_of(level):
        below = [i for i, s_ in enumerate(strikes) if s_ <= level]
        if not below or below[-1] >= n - 1:
            return None
        i = below[-1]
        lo, hi = strikes[i], strikes[i + 1]
        return i + (level - lo) / (hi - lo) if hi > lo else i

    placed = []
    for level, col, text, style in ((spot, T["accent"], f" narx {price_s(spot)} ", (0, (4, 3))),
                                    (gx["flip"], T["muted"], f" gamma flip {price_s(gx['flip'])} ", (0, (1, 2)))):
        y = y_of(level) if level else None
        if y is None:
            continue
        ax.axhline(y, color=col, linestyle=style, linewidth=1.3, zorder=3)
        left = any(abs(y - y0) < 1.0 for y0 in placed)              # too close to the price label: other side
        placed.append(y)
        ax.text(-span * 1.13 if left else span * 1.13, y, text, color=col, fontsize=8.5,
                ha="left" if left else "right", va="center", fontweight="bold", zorder=5,
                bbox=dict(boxstyle="round,pad=0.25,rounding_size=0.5", facecolor=T["bg"], edgecolor=col, linewidth=1))
    ax.text(-span * 1.13, n - 0.4, "◀ manfiy gamma", color=T["down"], fontsize=9.5, fontweight="bold", va="bottom")
    ax.text(span * 1.13, n - 0.4, "musbat gamma ▶", color=T["up"], fontsize=9.5, fontweight="bold", va="bottom",
            ha="right")
    fig.text(0.04, 0.32 / fig_h, "Strike bo'yicha dilerlar gammasi (net GEX, $)", fontsize=8, color=T["muted"])
    fig.text(0.96, 0.32 / fig_h, "Trading Laboratory · OptionWhales (~15 daq.)", fontsize=8, color=T["muted"], ha="right")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=T["bg"])
    plt.close(fig)
    return buf.getvalue()


def company_name(symbol):
    try:
        info = yf.Ticker(symbol).info or {}
        return info.get("longName") or info.get("shortName") or symbol
    except Exception:
        return symbol


def ow_report(symbol):
    """True when the OptionWhales report was sent."""
    if not OW_KEY:
        print("OW_API_KEY yo'q: Yahoo hisobotiga o'tamiz")
        return False
    ow = ow_collect(symbol)
    if not ow:
        print(f"{symbol}: OptionWhales returned nothing, Yahoo fallback")
        return False
    a = ow_analyse(symbol, ow)
    name = company_name(symbol)
    print(f"{symbol}: OptionWhales flow={bool(a.get('flow'))} gex={bool(a.get('gex'))} "
          f"orders={a.get('tape', {}).get('count', 0)} iv={bool(a.get('iv'))} dark={len(a.get('dark') or [])}")
    icon, word, hint, _ = flow_mood(a)
    cap = [f"🐋 <b>${esc(symbol)} — optsion oqimi</b>", "", f"{icon} Oqim: <b>{word}</b>, {hint}"]
    gx = a.get("gex")
    if gx and (gx["call_wall"] or gx["put_wall"]):
        cap += ["", f"🧲 Call wall {price_s(gx['call_wall'])} · Put wall {price_s(gx['put_wall'])}"]
    t = a.get("tape")
    if t and t["split"]["bull_share"] is not None:
        cap += ["", f"💰 O'sishga tikilgan pul {t['split']['bull_share'] * 100:.0f}%"]
    text = ow_text(name, a)
    png = gex_chart(name, a)
    global OW_SENT
    OW_SENT = True                              # from here on, never also send the Yahoo report
    parts = split_text(text)
    if png:
        send_photo(png, "\n".join(cap))
        for part in parts:
            send_text(part, reply=False, html_mode=True)
    else:
        send_text(parts[0], html_mode=True)
        for part in parts[1:]:
            send_text(part, reply=False, html_mode=True)
    return True


# ----------------------------- Main -----------------------------------------

def main():
    symbol = TICKER_IN.upper().lstrip("$").replace(".", "-")
    if not re.fullmatch(r"[A-Z][A-Z\-]{0,7}", symbol):
        send_text(f"❌ \"{TICKER_IN}\" — noto'g'ri tiker. Masalan: $TSLA")
        return
    try:
        if ow_report(symbol):
            return
    except Exception as exc:                    # never leave the user without an answer: Yahoo fallback
        print(f"OptionWhales report failed: {type(exc).__name__}: {exc}")
        if OW_SENT:
            raise
    d = collect(symbol)
    if not d:
        send_text(f"❌ ${symbol} topilmadi. Tikerni tekshiring (masalan: $AAPL, $TSLA).")
        return
    if not d["rows"]:
        send_text(f"ℹ️ ${symbol} ({d['name']}) bo'yicha optsionlar savdosi yo'q yoki ma'lumot topilmadi.")
        return
    if not any(r["vol"] for r in d["rows"]):
        send_text(f"ℹ️ ${symbol} bo'yicha bugun optsion savdosi bo'lmagan (bozor yopiq yoki savdo juda kam).")
        return
    a = analyse(d)
    print(f"{symbol}: {len(d['rows'])} contracts, {len(d['expiries'])} expiries, unusual {len(a['unusual'])}")
    word, _ = mood(a["call_share"])
    caption = (f"📊 <b>${esc(symbol)} — optsion faolligi</b>\n"
               f"Call/Put premium: <b>${big(a['call_prem'])} / ${big(a['put_prem'])}</b>\n"
               f"Kayfiyat: <b>{word}</b>")
    png = make_chart(d, a)
    if png:
        send_photo(png, caption)
        send_text(format_text(d, a), reply=False, html_mode=True)
    else:
        send_text(format_text(d, a), html_mode=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}")
        try:
            send_text(f"❌ ${TICKER_IN.upper().lstrip('$')} bo'yicha optsion ma'lumotini olib bo'lmadi. "
                      "Birozdan keyin qayta urinib ko'ring.")
        finally:
            delete_ack()
            raise SystemExit(1)
    delete_ack()
