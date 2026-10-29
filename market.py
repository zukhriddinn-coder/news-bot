"""
Trading Laboratory -> "Bozor holati" (market regime) bot
Every US trading day at 09:00 New York time (30 minutes before the open) Google Apps Script starts this
script on GitHub Actions. It posts one card + one message to the "Bozor holati" topic:
  - S&P 500 (SPY) and Nasdaq 100 (QQQ): price vs 50- and 200-day averages, yesterday's change
  - S&P 500 and Nasdaq futures: the move since yesterday's close (pre-market mood)
  - VIX: the market's fear gauge
  - Breadth: share of S&P 500 stocks above their 50- and 200-day averages
  - Sector heatmap: the 11 SPDR sector ETFs, yesterday and the last 5 days
  - A score (-8 ... +8) and a verdict: 🟢 good for LONG / 🟡 be careful / 🔴 not good for LONG
Daily bars only, closed candles only (today's bar is dropped before the open). Free data (Yahoo Finance).
This is a market regime summary, not a buy or sell signal.
"""

import html
import io
import json
import os
import time
import urllib.error
import urllib.request
import uuid

import numpy as np

import scanner as S                     # S&P 500 list, price download, New York clock
import buffetai as B                    # card theme and font

# ---------------------------------------------------------------------------
# SETTINGS
# ---------------------------------------------------------------------------

RUN_AT_NY = (9, 0)                      # 09:00 New York = 30 minutes before the open
RUN_WINDOW_MIN = 25                     # Apps Script may be a little late
GOOD_SCORE = 4                          # score >= 4: 🟢 good for LONG
BAD_SCORE = 0                           # score < 0: 🔴 not good for LONG; between: 🟡
NYSE_HOLIDAYS = {"2026-11-26", "2026-12-25", "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26",
                 "2027-05-31", "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24"}
INDEXES = [("SPY", "S&P 500"), ("QQQ", "Nasdaq 100")]
FUTURES = [("ES=F", "S&P 500"), ("NQ=F", "Nasdaq")]
SECTORS = [("XLK", "Texnologiya"), ("XLC", "Kommunikatsiya"), ("XLY", "Iste'mol (ixtiyoriy)"),
           ("XLP", "Iste'mol (asosiy)"), ("XLF", "Moliya"), ("XLV", "Sog'liqni saqlash"),
           ("XLI", "Sanoat"), ("XLE", "Energetika"), ("XLB", "Xomashyo"), ("XLU", "Kommunal xizmatlar"),
           ("XLRE", "Ko'chmas mulk")]

# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("MARKET_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("MARKET_CHAT_ID", "").strip() or "-1003382397608"
THREAD_ID = os.environ.get("MARKET_THREAD_ID", "").strip()
FORCE = os.environ.get("FORCE", "").strip().lower() in ("1", "true", "yes")
OYLAR = ["yanvar", "fevral", "mart", "aprel", "may", "iyun", "iyul", "avgust", "sentyabr", "oktyabr", "noyabr",
         "dekabr"]
GREEN, RED, YELLOW = "🟢", "🔴", "🟡"


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


def send(png, caption, text):
    tg("sendPhoto", {**where(), "caption": caption[:1024], "parse_mode": "HTML"},
       files={"photo": ("bozor_holati.png", png, "image/png")})
    tg("sendMessage", {**where(), "text": text, "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}})


# ----------------------------- Numbers --------------------------------------

def sma(x, n):
    return float(np.mean(x[-n:])) if len(x) >= n else None


def chg(c, k=1):
    return float(c[-1] / c[-1 - k] - 1) * 100 if len(c) > k else None


def index_state(df):
    c = df["Close"].to_numpy(dtype=float)
    ma50, ma200 = sma(c, 50), sma(c, 200)
    above50, above200 = c[-1] > ma50, c[-1] > ma200
    score = (1 if above50 else -1) + (1 if above200 else -1)
    if above50 and above200:
        txt = "50 va 200 kunlik o'rtachadan yuqori"
    elif above200:
        txt = "200 kunlikdan yuqori, lekin 50 kunlikdan past (qisqa muddatli zaiflik)"
    elif above50:
        txt = "50 kunlikdan yuqori, lekin 200 kunlikdan past (tiklanish urinishi)"
    else:
        txt = "50 va 200 kunlik o'rtachadan past"
    return {"price": float(c[-1]), "d1": chg(c), "w1": chg(c, 5), "ma50": ma50, "ma200": ma200,
            "above50": bool(above50), "above200": bool(above200), "score": score, "text": txt,
            "series": c[-130:], "ma50s": np.convolve(c, np.ones(50) / 50, "valid")[-130:],
            "ma200s": np.convolve(c, np.ones(200) / 200, "valid")[-130:]}


def vix_state(df):
    c = df["Close"].to_numpy(dtype=float)
    v = float(c[-1])
    if v < 15:
        score, txt = 2, "juda tinch bozor"
    elif v < 20:
        score, txt = 1, "tinch bozor"
    elif v < 25:
        score, txt = 0, "xavotir oshmoqda"
    elif v < 30:
        score, txt = -1, "qo'rquv yuqori"
    else:
        score, txt = -2, "vahima"
    return {"value": v, "d1": chg(c), "avg20": sma(c, 20), "score": score, "text": txt}


def breadth(prices):
    a50 = a200 = n = 0
    for df in prices.values():
        c = df["Close"].to_numpy(dtype=float)
        if len(c) < 200:
            continue
        n += 1
        a50 += c[-1] > sma(c, 50)
        a200 += c[-1] > sma(c, 200)
    if not n:
        return None
    p50, p200 = a50 / n * 100, a200 / n * 100
    score = (1 if p50 >= 60 else -1 if p50 <= 40 else 0) + (1 if p200 >= 60 else -1 if p200 <= 40 else 0)
    return {"p50": p50, "p200": p200, "n": n, "score": score}


def verdict(score):
    if score >= GOOD_SCORE:
        return GREEN, "LONG uchun qulay", "up"
    if score >= BAD_SCORE:
        return YELLOW, "Ehtiyotkorlik bilan", "wait"
    return RED, "LONG uchun noqulay", "down"


def dot(x, flat=0.3):
    return GREEN if x is not None and x > flat else RED if x is not None and x < -flat else YELLOW


def sp(x, digits=1):
    if x is None:
        return "—"
    return f"{'+' if x >= 0 else '−'}{abs(x):.{digits}f}%"


def uz_date(d):
    return f"{d.day} {OYLAR[d.month - 1]}"


# ----------------------------- Card -----------------------------------------

def make_card(day, st):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import FancyBboxPatch

    T = dict(B.THEME)
    plt.rcParams["font.family"] = B._font()
    W, H = 10, 10.9
    fig = plt.figure(figsize=(W, H), dpi=160, facecolor=T["bg"])
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_axis_off()
    ax.set_xlim(0, 1)
    ax.set_ylim(0, H)

    def panel(x, y, w, h):
        ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0,rounding_size=0.025",
                                    facecolor=T["panel"], edgecolor=T["line"], linewidth=1))

    icon, word, key = verdict(st["score"])
    col = T[key]
    ax.text(0.04, H - 0.55, "Bozor holati", fontsize=24, fontweight="bold", color=T["ink"], va="center")
    ax.text(0.04, H - 0.98, f"{day:%d.%m.%Y} · AQSh bozori ochilishidan oldin · kunlik grafik", fontsize=10,
            color=T["muted"], va="center")
    ax.text(0.955, H - 0.55, word, fontsize=15, fontweight="bold", color=col, va="center", ha="right",
            bbox=dict(boxstyle="round,pad=0.45,rounding_size=0.6", facecolor=T["panel"], edgecolor=col,
                      linewidth=1.4))
    ax.text(0.955, H - 1.05, f"ball {st['score']:+d} / 8", fontsize=9.5, color=T["muted"], va="center", ha="right")

    # indexes: price with 50 / 200-day averages
    for k, (sym, name) in enumerate(INDEXES):
        s = st["idx"].get(sym)
        x0 = 0.03 + k * 0.475
        top = H - 1.4
        panel(x0, top - 3.05, 0.465, 3.0)
        if not s:
            ax.text(x0 + 0.02, top - 0.35, f"{name}: ma'lumot yo'q", fontsize=11, color=T["muted"])
            continue
        c_ = T["up"] if s["score"] > 0 else T["down"] if s["score"] < 0 else T["wait"]
        ax.text(x0 + 0.02, top - 0.32, f"{name}", fontsize=13, fontweight="bold", color=T["ink"], va="center")
        ax.text(x0 + 0.02, top - 0.62, f"{sym} {s['price']:,.2f}  {sp(s['d1'])}", fontsize=9.5,
                color=T["up"] if (s["d1"] or 0) >= 0 else T["down"], va="center")
        ax.text(x0 + 0.445, top - 0.32, "●", fontsize=14, color=c_, va="center", ha="right")
        sx = fig.add_axes([x0 + 0.015, (top - 2.95) / H, 0.435, 2.1 / H], facecolor=T["panel"])
        n = len(s["series"])
        sx.plot(range(n), s["series"], color=T["ink"], linewidth=1.3)
        sx.plot(range(n - len(s["ma50s"]), n), s["ma50s"], color=T["e20"], linewidth=1.1)
        sx.plot(range(n - len(s["ma200s"]), n), s["ma200s"], color=T["e50"], linewidth=1.1)
        sx.set_axis_off()
        ax.text(x0 + 0.445, top - 0.62, "MA 200", fontsize=8, color=T["e50"], va="center", ha="right")
        ax.text(x0 + 0.37, top - 0.62, "MA 50", fontsize=8, color=T["e20"], va="center", ha="right")

    # three tiles: VIX, breadth, futures
    top = H - 4.65
    tiles = []
    v = st.get("vix")
    if v:
        vc = T["up"] if v["score"] > 0 else T["down"] if v["score"] < 0 else T["wait"]
        tiles.append(("VIX · qo'rquv indeksi", f"{v['value']:.1f}", vc, f"{v['text']} · {sp(v['d1'])}"))
    b = st.get("breadth")
    if b:
        bc = T["up"] if b["score"] > 0 else T["down"] if b["score"] < 0 else T["wait"]
        tiles.append(("S&P 500 kengligi", f"{b['p50']:.0f}%", bc,
                      f"50 kunlikdan yuqori · 200 kunlikdan {b['p200']:.0f}%"))
    fu = st.get("fut") or {}
    if fu:
        avg = np.mean([x for x in fu.values() if x is not None]) if any(x is not None for x in fu.values()) else None
        fc = T["up"] if (avg or 0) > 0.3 else T["down"] if (avg or 0) < -0.3 else T["wait"]
        tiles.append(("Fyuchers hozir", " / ".join(sp(fu.get(n_)) for _, n_ in FUTURES), fc,
                      "S&P 500 / Nasdaq · kechagi yopilishdan"))
    for k, (title, big, c_, sub) in enumerate(tiles):
        x0 = 0.03 + k * 0.317
        panel(x0, top - 1.45, 0.307, 1.4)
        ax.text(x0 + 0.02, top - 0.3, title, fontsize=9, color=T["muted"], va="center")
        ax.text(x0 + 0.02, top - 0.78, big, fontsize=19 if len(big) < 10 else 15, fontweight="bold", color=c_,
                va="center")
        ax.text(x0 + 0.02, top - 1.18, sub, fontsize=7.6, color=T["muted"], va="center")

    # sector heatmap: yesterday's change; the last 5 days below
    top = H - 6.35
    ax.text(0.04, top - 0.15, "Sektorlar · kechagi o'zgarish (5 kunlik pastda)", fontsize=10, color=T["muted"],
            va="center")
    secs = sorted(st["sectors"], key=lambda r: -(r["d1"] or -99))
    cells = secs + ([{"sym": "SPY", "name": "S&P 500", "d1": st["idx"]["SPY"]["d1"], "w1": st["idx"]["SPY"]["w1"]}]
                    if st["idx"].get("SPY") else [])
    cols, gap, chh = 4, 0.012, 0.98
    cw = (0.94 - (cols - 1) * gap) / cols
    for k, r in enumerate(cells):
        cx = 0.03 + (k % cols) * (cw + gap)
        cy = top - 0.45 - (k // cols + 1) * (chh + 0.1)
        d = r["d1"] or 0
        strength = min(abs(d) / 2.0, 1.0)
        base = T["up"] if d > 0.3 else T["down"] if d < -0.3 else T["wait"]
        alpha = 0.18 + 0.55 * strength if abs(d) > 0.3 else 0.22
        ax.add_patch(FancyBboxPatch((cx, cy), cw, chh, boxstyle="round,pad=0,rounding_size=0.02",
                                    facecolor=base, alpha=alpha, edgecolor="none"))
        ax.add_patch(FancyBboxPatch((cx, cy), cw, chh, boxstyle="round,pad=0,rounding_size=0.02",
                                    facecolor="none", edgecolor=T["line"], linewidth=1))
        ax.text(cx + 0.012, cy + chh - 0.22, r["name"], fontsize=8.6, color=T["ink"], va="center",
                fontweight="bold" if r["sym"] == "SPY" else "normal")
        ax.text(cx + 0.012, cy + 0.42, sp(r["d1"]), fontsize=15, fontweight="bold", color=T["ink"], va="center")
        ax.text(cx + 0.012, cy + 0.14, f"{r['sym']} · 5 kun {sp(r['w1'])}", fontsize=7.5, color=T["ink"],
                alpha=0.8, va="center")

    ax.text(0.04, 0.28, "Bozor rejimi ko'rsatkichi — savdo signali emas", fontsize=8, color=T["muted"], va="center")
    ax.text(0.96, 0.28, "Trading Laboratory · Investitsion maslahat emas", fontsize=8, color=T["muted"],
            va="center", ha="right")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=T["bg"])
    plt.close(fig)
    return buf.getvalue()


# ----------------------------- Text -----------------------------------------

def esc(s):
    return html.escape(str(s), quote=False)


def summary(st):
    parts = []
    spy, qqq = st["idx"].get("SPY"), st["idx"].get("QQQ")
    if spy and qqq:
        if spy["score"] == 2 and qqq["score"] == 2:
            parts.append("S&P 500 ham, Nasdaq ham 50 va 200 kunlik o'rtachadan yuqori: asosiy trend o'sish")
        elif spy["score"] == -2 and qqq["score"] == -2:
            parts.append("S&P 500 ham, Nasdaq ham 50 va 200 kunlik o'rtachadan past: asosiy trend pasayish")
        else:
            parts.append("indekslarda trend aralash")
    v = st.get("vix")
    if v:
        parts.append(f"VIX {v['value']:.1f}, {v['text']}")
    b = st.get("breadth")
    if b:
        parts.append(f"S&P 500 aksiyalarining {b['p50']:.0f}% i 50 kunlik o'rtachadan yuqori")
    s = "; ".join(parts)
    return s[0].upper() + s[1:] + "." if s else ""


ADVICE = {GREEN: "LONG setaplar uchun qulay muhit. Odatdagi pozitsiya hajmi, skaner nomzodlariga e'tibor bering.",
          YELLOW: "Bozor ikkilanmoqda. Pozitsiyani kichikroq oling, faqat kuchli setaplarni tanlang.",
          RED: "LONG uchun noqulay muhit. Yangi LONG'larni kechiktiring yoki juda kichik hajmda oling."}


def format_text(day, st):
    icon, word, _ = verdict(st["score"])
    lines = ["🌡 <b>Bozor holati</b>", f"<i>{uz_date(day)} {day.year}, AQSh bozori ochilishidan 30 daqiqa oldin</i>",
             "", f"{icon} <b>{word}</b>  <i>ball {st['score']:+d} / 8</i>"]
    sm = summary(st)
    if sm:
        lines.append(f"<blockquote>{esc(sm)}</blockquote>")
    lines.append("")

    lines.append("📈 <b>Indekslar</b>")
    for sym, name in INDEXES:
        s = st["idx"].get(sym)
        if s:
            lines.append(f"{GREEN if s['score'] > 0 else RED if s['score'] < 0 else YELLOW} {esc(name)} ({sym}) <b>{s['price']:,.2f}</b>"
                         f" <i>{sp(s['d1'])}</i>, {s['text']}")
    fu = st.get("fut") or {}
    if any(x is not None for x in fu.values()):
        lines.append("Fyuchers hozir: " + ", ".join(f"{esc(n_)} <i>{sp(fu.get(n_))}</i>" for _, n_ in FUTURES if fu.get(n_) is not None))
    lines.append("")

    v = st.get("vix")
    if v:
        lines += [f"{GREEN if v['score'] > 0 else RED if v['score'] < 0 else YELLOW} <b>VIX</b> <b>{v['value']:.1f}</b> "
                  f"<i>{sp(v['d1'])}</i>, {v['text']}"]
    b = st.get("breadth")
    if b:
        lines.append(f"{GREEN if b['score'] > 0 else RED if b['score'] < 0 else YELLOW} <b>Bozor kengligi:</b> S&amp;P 500 "
                     f"aksiyalarining <b>{b['p50']:.0f}%</b> i 50 kunlik, <b>{b['p200']:.0f}%</b> i 200 kunlik o'rtachadan yuqori")
    lines.append("")

    secs = sorted(st["sectors"], key=lambda r: -(r["d1"] or -99))
    lines.append("🗺 <b>Sektorlar</b>  <i>kecha, 5 kun</i>")
    for r in secs:
        lines.append(f"{dot(r['d1'])} {r['name']} <b>{sp(r['d1'])}</b>  <i>{sp(r['w1'])}</i>")
    if secs:
        lead = sorted(secs, key=lambda r: -(r["w1"] or -99))
        lines.append(f"<i>5 kunlik yetakchi: {lead[0]['name']}. Eng zaif: {lead[-1]['name']}.</i>")
    lines += ["", f"💡 <b>Bugun uchun:</b> {ADVICE[icon]}"]
    vals = [x for x in fu.values() if x is not None]
    if vals and min(vals) <= -0.7:
        lines.append(f"⚠️ Fyuchers bugun pasayish bilan ochilishni ko'rsatmoqda ({', '.join(f'{esc(n_)} {sp(fu.get(n_))}' for _, n_ in FUTURES if fu.get(n_) is not None)}). "
                     "Birinchi 30 daqiqada shoshilmang.")
    elif vals and max(vals) >= 0.7 and icon != GREEN:
        lines.append("ℹ️ Fyuchers bugun o'sish bilan ochilishni ko'rsatmoqda, lekin umumiy fon hali zaif.")
    lines.append("")
    if st.get("partial"):
        lines += [f"<i>ℹ️ {esc(st['partial'])}</i>", ""]
    lines.append("<i>Ball: indekslar trendi (±4), VIX (±2), bozor kengligi (±2). Bu bozor rejimi ko'rsatkichi, "
                 "savdo signali emas. Investitsion maslahat emas.</i>")
    return "\n".join(lines)


# ----------------------------- Main -----------------------------------------

def main():
    if not BOT_TOKEN:
        raise SystemExit("XATO: MARKET_BOT_TOKEN secret topilmadi — Settings → Secrets and variables → Actions")
    now = S.ny_now()
    day = now.strftime("%Y-%m-%d")
    if not FORCE:
        target = now.replace(hour=RUN_AT_NY[0], minute=RUN_AT_NY[1], second=0, microsecond=0)
        if now.weekday() >= 5 or day in NYSE_HOLIDAYS or not (
                -5 <= (now - target).total_seconds() / 60 <= RUN_WINDOW_MIN):
            print(f"New York {now:%Y-%m-%d %H:%M}: not the time, nothing to do")
            return
    pre_open = now.hour * 60 + now.minute < 9 * 60 + 30

    def closed(df):
        """Before the open, drop a partial bar for today: closed candles only."""
        return df[df.index.date < now.date()] if pre_open else df

    syms = [s for s, _ in INDEXES] + [s for s, _ in SECTORS]
    data = S.download(syms, period="2y", min_bars=210)
    idx = {s: index_state(closed(data[s])) for s, _ in INDEXES if s in data}
    sectors = []
    for s, name in SECTORS:
        if s in data:
            c = closed(data[s])["Close"].to_numpy(dtype=float)
            sectors.append({"sym": s, "name": name, "d1": chg(c), "w1": chg(c, 5)})
    vx = S.download(["^VIX"], period="6mo", min_bars=30).get("^VIX")
    vix = vix_state(closed(vx)) if vx is not None else None     # VIX: yesterday's close, like the other numbers
    fut = {}
    fdata = S.download([s for s, _ in FUTURES], period="1mo", min_bars=3)
    for s, name in FUTURES:
        if s in fdata:
            fut[name] = chg(fdata[s]["Close"].to_numpy(dtype=float))
    members = S.sp500()
    prices = S.download(list(members), period="1y", min_bars=200)
    br = breadth({k: closed(v) for k, v in prices.items()})

    score = sum(s["score"] for s in idx.values()) + (vix["score"] if vix else 0) + (br["score"] if br else 0)
    st = {"idx": idx, "sectors": sectors, "vix": vix, "breadth": br, "fut": fut, "score": score}
    if len(prices) < len(members) * 0.8:
        st["partial"] = f"Bozor kengligi {len(members)} aksiyadan {len(prices)} tasi bo'yicha hisoblandi."
    if not idx:
        raise SystemExit("Index prices could not be downloaded")
    icon, word, _ = verdict(score)
    caption = f"🌡 <b>Bozor holati</b> · {uz_date(now)}\n{icon} <b>{word}</b>, ball {score:+d} / 8"
    send(make_card(now, st), caption, format_text(now, st))
    print(f"Sent: score {score}, {word}")


if __name__ == "__main__":
    main()
