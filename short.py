"""
Trading Laboratory -> Short interest
A subscriber writes $TICKER in the Short interest topic. Google Apps Script starts this script (GitHub Actions).
It collects:
  - short interest from Yahoo Finance (exchanges report it twice a month): shares sold short, % of float,
    days to cover, change against the previous report;
  - daily short sale volume of the last 20 trading days from FINRA's free daily files
    (https://cdn.finra.org/equity/regsho/daily/CNMSshvolYYYYMMDD.txt);
  - recent price move;
and replies with a chart card + text with a simple short squeeze risk score (0-100).
"""

import html
import io
import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import yfinance as yf

# ---------------------------------------------------------------------------
# SETTINGS (you can edit these)
# ---------------------------------------------------------------------------

FINRA_DAYS = 20               # trading days of daily short volume
FINRA_URL = "https://cdn.finra.org/equity/regsho/daily/CNMSshvol{d}.txt"
DISCLAIMER = ("⚠️ Investitsion maslahat emas. Manba: Yahoo Finance (birjalar short interest'ni oyiga 2 marta "
              "e'lon qiladi) va FINRA kunlik fayllari. Squeeze xavfi — oddiy formula, bashorat emas.")

# ---------------------------------------------------------------------------

BOT_TOKEN = re.sub(r"\s+", "", os.environ.get("SHORT_BOT_TOKEN", ""))
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
    p = {**where(reply), "text": text[:4000], "link_preview_options": {"is_disabled": True}}
    if html_mode:
        p["parse_mode"] = "HTML"
    tg("sendMessage", p)


def send_photo(png, caption):
    tg("sendPhoto", {**where(), "caption": caption[:1024], "parse_mode": "HTML"},
       files={"photo": ("short.png", png, "image/png")})


def delete_ack():
    if ACK_ID:
        try:
            tg("deleteMessage", {"chat_id": CHAT_ID, "message_id": int(ACK_ID)})
        except Exception as exc:
            print(f"Could not delete the waiting message: {exc}")


# ----------------------------- Data -----------------------------------------

def finra_day(day, symbols):
    """Returns (date, short volume, total volume) for the symbol, or None (holiday / missing)."""
    url = FINRA_URL.format(d=day.strftime("%Y%m%d"))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 TradingLaboratory bot"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode("utf-8", "replace")
    except Exception:
        return None
    for line in text.splitlines():
        parts = line.split("|")
        if len(parts) >= 5 and parts[1] in symbols:
            try:
                return day.date(), float(parts[2]), float(parts[4])
            except ValueError:
                return None
    return None


def finra_series(symbol):
    symbols = {symbol, symbol.replace("-", "."), symbol.replace("-", "/"), symbol.replace("-", " ")}
    today = datetime.now(timezone.utc)
    days = [today - timedelta(days=k) for k in range(0, 40)]
    days = [d for d in days if d.weekday() < 5]
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = [r for r in pool.map(lambda d: finra_day(d, symbols), days) if r]
    rows.sort()
    return rows[-FINRA_DAYS:]


def collect(symbol):
    t = yf.Ticker(symbol)
    info = t.info or {}
    hist = t.history(period="6mo", interval="1d", auto_adjust=False)
    if not info.get("quoteType") and (hist is None or hist.empty):
        return None
    close = hist["Close"].dropna() if hist is not None and not hist.empty else None
    price = float(close.iloc[-1]) if close is not None and len(close) else info.get("currentPrice")

    def ret(n):
        return float(close.iloc[-1] / close.iloc[-1 - n] - 1) if close is not None and len(close) > n else None

    short = info.get("sharesShort")
    prior = info.get("sharesShortPriorMonth")
    flt = info.get("floatShares")
    pct = info.get("shortPercentOfFloat")
    if pct is None and short and flt:
        pct = short / flt
    avg_vol = info.get("averageVolume") or info.get("averageDailyVolume3Month")
    dtc = info.get("shortRatio") or (short / avg_vol if short and avg_vol else None)

    def day(ts):
        return datetime.fromtimestamp(ts, timezone.utc).date() if ts else None

    return {
        "name": info.get("longName") or info.get("shortName") or symbol,
        "type": info.get("quoteType", ""), "price": price,
        "short": short, "prior": prior, "pct": pct, "dtc": dtc, "float": flt, "avg_vol": avg_vol,
        "pct_out": info.get("sharesPercentSharesOut"),
        "date": day(info.get("dateShortInterest")), "prior_date": day(info.get("sharesShortPreviousMonthDate")),
        "ret_1m": ret(21), "ret_3m": ret(63), "ret_5d": ret(5),
        "price_series": [(d.date(), float(v)) for d, v in close.items()] if close is not None else [],
    }


# ----------------------------- Analysis -------------------------------------

def squeeze(d, fin):
    """Simple 0-100 score from short % of float, days to cover, trend of short interest, price momentum
    and recent daily short volume. Returns (score, label, reasons)."""
    score, why = 0.0, []
    pct = (d["pct"] or 0) * 100
    if pct:
        score += min(45, pct * 1.5)
        if pct >= 20:
            why.append(f"floatning {pct:.0f}% i shortda — juda yuqori")
        elif pct >= 10:
            why.append(f"floatning {pct:.0f}% i shortda — yuqori")
    dtc = d["dtc"] or 0
    if dtc:
        score += min(25, dtc * 3)
        if dtc >= 5:
            why.append(f"shortlarni yopish uchun {dtc:.1f} kun kerak")
    if d["short"] and d["prior"]:
        ch = d["short"] / d["prior"] - 1
        score += max(-10, min(15, ch * 60))
        if ch >= 0.1:
            why.append(f"short pozitsiyalar {ch * 100:+.0f}% oshgan")
        elif ch <= -0.1:
            why.append(f"shortlar yopilmoqda ({ch * 100:+.0f}%)")
    r1 = d["ret_1m"]
    if r1 is not None and pct >= 10 and r1 > 0:
        score += min(15, r1 * 50)
        if r1 >= 0.15:
            why.append(f"narx 1 oyda {r1 * 100:+.0f}% — shortlar zarar ko'rmoqda")
    if fin and len(fin) >= 10:
        r5 = sum(s for _, s, _ in fin[-5:]) / max(sum(t for _, _, t in fin[-5:]), 1)
        r20 = sum(s for _, s, _ in fin) / max(sum(t for _, _, t in fin), 1)
        if r5 - r20 >= 0.05:
            score += 5
            why.append("so'nggi 5 kunda kunlik short hajmi oshgan")
    score = int(max(0, min(100, round(score))))
    label = ("JUDA YUQORI" if score >= 70 else "YUQORI" if score >= 50 else "O'RTA" if score >= 25 else "PAST")
    return score, label, why


# ----------------------------- Formatting -----------------------------------

def big(v):
    if v is None:
        return "—"
    a = abs(v)
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            return f"{v / div:,.1f}{suf}"
    return f"{v:,.0f}"


def esc(s):
    return html.escape(str(s), quote=False)


def pc(v, plus=False):
    return "—" if v is None else (f"{v * 100:+.1f}%" if plus else f"{v * 100:.1f}%")


def dmy(d):
    return d.strftime("%d.%m.%Y") if d else "—"


def finra_stats(fin):
    if not fin:
        return None
    ratios = [s / t for _, s, t in fin if t]
    r20 = sum(s for _, s, _ in fin) / max(sum(t for _, _, t in fin), 1)
    r5 = sum(s for _, s, _ in fin[-5:]) / max(sum(t for _, _, t in fin[-5:]), 1)
    return {"last": ratios[-1] if ratios else None, "r5": r5, "r20": r20, "days": len(fin),
            "last_date": fin[-1][0], "last_short": fin[-1][1], "max": max(ratios) if ratios else None}


def format_text(symbol, d, fin, sq):
    score, label, why = sq
    icon = {"JUDA YUQORI": "🔥", "YUQORI": "🟠", "O'RTA": "🟡", "PAST": "🟢"}[label]
    lines = [f"🩳 <b>${esc(symbol)} — short interest</b>",
             f"<i>{esc(d['name'])}" + (f" · narx ${d['price']:,.2f}</i>" if d["price"] else "</i>"), "",
             f"{icon} <b>Short squeeze xavfi: {label} ({score}/100)</b>"]
    if why:
        lines.append("   " + "; ".join(esc(w) for w in why))
    lines += ["", f"📊 <b>Short interest</b> (holat: {dmy(d['date'])})"]
    if d["short"]:
        ch = f" · oldingi ({dmy(d['prior_date'])}): {big(d['prior'])}" if d["prior"] else ""
        if d["prior"]:
            ch += f" ({pc(d['short'] / d['prior'] - 1, True)})"
        lines.append(f"   Short aksiyalar: <b>{big(d['short'])}</b>{ch}")
        lines.append(f"   Floatning: <b>{pc(d['pct'])}</b>" + (f" · barcha aksiyalarning {pc(d['pct_out'])}"
                                                                  if d["pct_out"] else ""))
        if d["dtc"]:
            lines.append(f"   Days to cover: <b>{d['dtc']:.1f} kun</b> (o'rtacha kunlik hajm {big(d['avg_vol'])})")
    else:
        lines.append("   Bu tiker uchun short interest ma'lumoti yo'q (ETF, xorijiy yoki yangi aksiya bo'lishi mumkin).")

    st = finra_stats(fin)
    lines += ["", f"📅 <b>Kunlik short hajmi</b> (FINRA, oxirgi {st['days'] if st else 0} kun)"]
    if st:
        trend = ("oshmoqda" if st["r5"] - st["r20"] >= 0.03 else "kamaymoqda" if st["r20"] - st["r5"] >= 0.03
                 else "barqaror")
        lines += [f"   Oxirgi kun ({dmy(st['last_date'])}): <b>{pc(st['last'])}</b> · {big(st['last_short'])} aksiya",
                  f"   5 kunlik o'rtacha: {pc(st['r5'])} · 20 kunlik: {pc(st['r20'])} → {trend}",
                  "   <i>Bu ochiq short pozitsiyalar emas: market-makerlarning savdolari ham kiradi, "
                  "40–50% odatiy holat.</i>"]
    else:
        lines.append("   FINRA ma'lumoti topilmadi.")

    lines += ["", "📈 <b>Narx</b>",
              f"   5 kun: {pc(d['ret_5d'], True)} · 1 oy: {pc(d['ret_1m'], True)} · 3 oy: {pc(d['ret_3m'], True)}",
              "", "🧭 <b>Qanday o'qish</b>",
              "   Float: &lt;5% past · 5–10% o'rta · 10–20% yuqori · 20%+ juda yuqori",
              "   Days to cover: 1–3 normal · 5+ squeeze uchun qulay",
              "   Short squeeze: narx ko'tarilsa, shortchilar yopish uchun sotib oladi va narx yana tez o'sadi.",
              "", f"<i>{esc(DISCLAIMER)}</i>"]
    return "\n".join(lines)


# ----------------------------- Chart ----------------------------------------

THEME = {"bg": "#0b1220", "panel": "#111a2e", "line": "#1e2a44", "ink": "#e6edf7", "muted": "#8a99b8",
         "short": "#f59e0b", "price": "#38bdf8", "JUDA YUQORI": "#ef4444", "YUQORI": "#f97316",
         "O'RTA": "#eab308", "PAST": "#22c55e"}


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


def make_chart(symbol, d, fin, sq):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import FancyBboxPatch

    T = THEME
    plt.rcParams["font.family"] = _font()
    score, label, _ = sq
    fig = plt.figure(figsize=(10, 8.2), dpi=160, facecolor=T["bg"])
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_axis_off()
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 8.2)
    ax.text(0.04, 7.65, f"${symbol}", fontsize=28, fontweight="bold", color=T["ink"], va="center")
    ax.text(0.04, 7.17, d["name"][:44], fontsize=12.5, color=T["ink"], va="center")
    ax.text(0.04, 6.82, f"Short interest · holat {dmy(d['date'])} · Yahoo + FINRA", fontsize=9.5,
            color=T["muted"], va="center")

    # squeeze gauge
    col = T[label]
    ax.text(0.60, 7.72, "Short squeeze xavfi", fontsize=9.5, color=T["muted"], va="center")
    ax.text(0.60, 7.28, f"{score}", fontsize=26, fontweight="bold", color=col, va="center")
    ax.text(0.60 + 0.024 * len(str(score)) + 0.006, 7.2, "/100", fontsize=11, color=T["muted"], va="center")
    ax.text(0.955, 7.28, label, fontsize=13, fontweight="bold", color=col, va="center", ha="right")
    ax.add_patch(FancyBboxPatch((0.60, 6.82), 0.355, 0.09, boxstyle="round,pad=0,rounding_size=0.04",
                                facecolor=T["line"], edgecolor="none"))
    ax.add_patch(FancyBboxPatch((0.60, 6.82), max(0.355 * score / 100, 0.01), 0.09,
                                boxstyle="round,pad=0,rounding_size=0.04", facecolor=col, edgecolor="none"))

    # KPI tiles
    ch = (d["short"] / d["prior"] - 1) if d["short"] and d["prior"] else None
    tiles = [("Floatning shortda", pc(d["pct"]), "20%+ = juda yuqori"),
             ("Days to cover", f"{d['dtc']:.1f} kun" if d["dtc"] else "—", "5+ = squeeze uchun qulay"),
             ("Short aksiyalar", big(d["short"]), f"oldingi hisobotga {pc(ch, True)}" if ch is not None else ""),
             ("Narx, 1 oy", pc(d["ret_1m"], True), f"3 oy: {pc(d['ret_3m'], True)}")]
    for k, (t, v, s) in enumerate(tiles):
        x = 0.04 + k * 0.235
        ax.add_patch(FancyBboxPatch((x, 5.2), 0.215, 1.28, boxstyle="round,pad=0,rounding_size=0.04",
                                    facecolor=T["panel"], edgecolor=T["line"], linewidth=1))
        ax.text(x + 0.02, 6.2, t, fontsize=9, color=T["muted"], va="center")
        ax.text(x + 0.02, 5.78, v, fontsize=18, fontweight="bold", color=T["ink"], va="center")
        ax.text(x + 0.02, 5.4, s, fontsize=8, color=T["muted"], va="center")

    # daily short volume bars + price line
    ax.text(0.04, 4.85, "■ Kunlik short hajmi, % (FINRA, chap o'q)", fontsize=9.5, color=T["short"], va="center")
    ax.text(0.42, 4.85, "— Narx, $ (o'ng o'q)", fontsize=9.5, color=T["price"], va="center")
    bx = fig.add_axes([0.08, 0.1, 0.84, 0.46], facecolor=T["bg"])
    if fin:
        dates = [x[0] for x in fin]
        ratios = [s / t * 100 if t else 0 for _, s, t in fin]
        xs = range(len(dates))
        bx.bar(xs, ratios, color=T["short"], width=0.62, alpha=0.9)
        bx.axhline(50, color=T["muted"], linewidth=0.8, linestyle="--")
        bx.text(-0.9, 51.5, "50%", color=T["muted"], fontsize=8, ha="left")
        bx.set_ylim(0, max(80, max(ratios) * 1.15))
        bx.set_xticks(list(xs)[::4], [dt.strftime("%d.%m") for dt in dates][::4])
        prices = dict(d["price_series"])
        py = [prices.get(dt) for dt in dates]
        if any(p is not None for p in py):
            px = bx.twinx()
            px.plot([i for i, p in zip(xs, py) if p is not None], [p for p in py if p is not None],
                    color=T["price"], linewidth=2)
            px.tick_params(colors=T["price"], labelsize=8.5, length=0)
            for sp in px.spines.values():
                sp.set_visible(False)
    else:
        bx.text(0.5, 0.5, "FINRA ma'lumoti topilmadi", transform=bx.transAxes, ha="center", color=T["muted"])
        bx.set_xticks([])
    bx.tick_params(colors=T["muted"], labelsize=8.5, length=0)
    bx.grid(axis="y", color=T["line"], linewidth=0.6)
    for sp in bx.spines.values():
        sp.set_visible(False)
    ax.text(0.04, 0.3, "Kunlik short hajmi ≠ ochiq shortlar: market-makerlar ham kiradi, 40–50% odatiy",
            fontsize=8, color=T["muted"], va="center")
    ax.text(0.96, 0.3, "Trading Laboratory · Investitsion maslahat emas", fontsize=8, color=T["muted"],
            va="center", ha="right")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=T["bg"])
    plt.close(fig)
    return buf.getvalue()


# ----------------------------- Main -----------------------------------------

def main():
    symbol = TICKER_IN.upper().lstrip("$").replace(".", "-")
    if not re.fullmatch(r"[A-Z][A-Z\-]{0,7}", symbol):
        send_text(f"❌ \"{TICKER_IN}\" — noto'g'ri tiker. Masalan: $GME")
        return
    d = collect(symbol)
    if not d:
        send_text(f"❌ ${symbol} topilmadi. Tikerni tekshiring.")
        return
    fin = finra_series(symbol)
    sq = squeeze(d, fin)
    print(f"{symbol}: short {d['short']} pct {d['pct']} dtc {d['dtc']} finra days {len(fin)} score {sq[0]}")
    if not d["short"] and not fin:
        send_text(f"ℹ️ ${symbol} ({d['name']}) uchun short ma'lumoti topilmadi. Short interest faqat AQSh "
                  "birjalaridagi aksiyalar uchun e'lon qilinadi.")
        return
    caption = (f"🩳 <b>${esc(symbol)} — short interest</b>\n"
               f"Floatning {pc(d['pct'])} i shortda · days to cover {d['dtc']:.1f}\n" if d["dtc"] else
               f"🩳 <b>${esc(symbol)} — short interest</b>\nFloatning {pc(d['pct'])} i shortda\n")
    caption += f"Squeeze xavfi: <b>{sq[1]} ({sq[0]}/100)</b>"
    send_photo(make_chart(symbol, d, fin, sq), caption)
    send_text(format_text(symbol, d, fin, sq), reply=False, html_mode=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}")
        try:
            send_text(f"❌ ${TICKER_IN.upper().lstrip('$')} bo'yicha ma'lumot olib bo'lmadi. "
                      "Birozdan keyin qayta urinib ko'ring.")
        finally:
            delete_ack()
            raise SystemExit(1)
    delete_ack()
