"""
Trading Laboratory -> unusual options activity for one ticker
A subscriber writes $TICKER in the Options topic. Google Apps Script starts this script
(GitHub Actions). It reads the option chains from Yahoo Finance (about 15 minutes delayed)
and replies with:
  - the overall flow (call vs put volume and premium, put/call ratio, implied volatility,
    expected move, max pain),
  - the most unusual contracts (today's volume far above open interest),
  - a chart of volume by strike (calls vs puts).
Note: free data has no trade-by-trade detail, so buy/sell side, sweeps and blocks are unknown.
"""

import html
import io
import json
import math
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from datetime import date, datetime
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

# ---------------------------------------------------------------------------

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


# ----------------------------- Main -----------------------------------------

def main():
    symbol = TICKER_IN.upper().lstrip("$").replace(".", "-")
    if not re.fullmatch(r"[A-Z][A-Z\-]{0,7}", symbol):
        send_text(f"❌ \"{TICKER_IN}\" — noto'g'ri tiker. Masalan: $TSLA")
        return
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
