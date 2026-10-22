"""
Trading Laboratory -> analyst price targets for one ticker
A subscriber writes $TICKER in the Targets topic. Google Apps Script starts this script
(GitHub Actions) with the ticker. It collects from Yahoo Finance:
  - the analyst consensus (mean / median / low / high target, recommendation),
  - the latest analyst actions (firm, rating, old -> new target),
and replies in the topic with a chart card and a formatted text card.
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
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import yfinance as yf

# ---------------------------------------------------------------------------
# SETTINGS (you can edit these)
# ---------------------------------------------------------------------------

MAX_ACTIONS = 10              # analyst actions in the text
CHART_ACTIONS = 8             # analyst actions on the chart
DAYS = 90                     # how far back to look for analyst actions
LOCAL_TZ = "Asia/Tashkent"    # dates are shown in this time zone
DISCLAIMER = "⚠️ Bu investitsion maslahat emas. Ma'lumotlar Yahoo Finance'dan olingan."

# ---------------------------------------------------------------------------

BOT_TOKEN = re.sub(r"\s+", "", os.environ.get("TARGETS_BOT_TOKEN", ""))
TICKER_IN = os.environ.get("TICKER", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()
THREAD_ID = os.environ.get("THREAD_ID", "").strip()
REPLY_TO = os.environ.get("REPLY_TO", "").strip()
ACK_ID = os.environ.get("ACK_ID", "").strip()

RECOMMENDATION = {
    "strong_buy": "Kuchli sotib olish 🟢", "buy": "Sotib olish 🟢", "hold": "Ushlab turish 🟡",
    "underperform": "Bozordan past 🔴", "sell": "Sotish 🔴", "strong_sell": "Kuchli sotish 🔴",
}
ACTION = {
    "up": "reytingni ko'tardi ⬆️", "down": "reytingni tushirdi ⬇️", "init": "kuzatishni boshladi 🆕",
    "main": "reytingni saqladi", "reit": "reytingni tasdiqladi",
}


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
       files={"photo": ("targets.png", png, "image/png")})


def delete_ack():
    if ACK_ID:
        try:
            tg("deleteMessage", {"chat_id": CHAT_ID, "message_id": int(ACK_ID)})
        except Exception as exc:
            print(f"Could not delete the waiting message: {exc}")


# ----------------------------- Yahoo Finance --------------------------------

def num(v):
    try:
        v = float(v)
        return None if math.isnan(v) or math.isinf(v) or v <= 0 else v
    except (TypeError, ValueError):
        return None


def collect(symbol):
    t = yf.Ticker(symbol)
    info = t.info or {}
    name = info.get("longName") or info.get("shortName")
    price = num(info.get("currentPrice")) or num(info.get("regularMarketPrice"))
    if not name or not price:
        return None

    targets = {}
    try:
        targets = t.analyst_price_targets or {}
    except Exception as exc:
        print(f"Yahoo targets: {exc}")
    data = {
        "symbol": symbol, "name": name, "price": price,
        "mean": num(targets.get("mean")) or num(info.get("targetMeanPrice")),
        "median": num(targets.get("median")) or num(info.get("targetMedianPrice")),
        "low": num(targets.get("low")) or num(info.get("targetLowPrice")),
        "high": num(targets.get("high")) or num(info.get("targetHighPrice")),
        "analysts": info.get("numberOfAnalystOpinions"),
        "rec_key": info.get("recommendationKey"),
        "rec_mean": num(info.get("recommendationMean")),
        "ratings": None, "actions": [],
    }

    try:
        rec = t.recommendations
        if rec is not None and not rec.empty:
            row = rec[rec["period"] == "0m"].iloc[0] if "period" in rec.columns and (rec["period"] == "0m").any() \
                else rec.iloc[0]
            data["ratings"] = {k: int(row.get(k, 0) or 0)
                               for k in ("strongBuy", "buy", "hold", "sell", "strongSell")}
    except Exception as exc:
        print(f"Yahoo recommendations: {exc}")

    try:
        df = t.upgrades_downgrades
    except Exception as exc:
        print(f"Yahoo upgrades/downgrades: {exc}")
        df = None
    if df is not None and len(df):
        now = time.time()
        for r in df.reset_index().to_dict("records"):
            try:
                ts = int(r["GradeDate"].timestamp())
            except Exception:
                continue
            if now - ts > DAYS * 86400:
                continue
            data["actions"].append({
                "ts": ts, "firm": str(r.get("Firm") or "").strip(),
                "to": str(r.get("ToGrade") or "").strip(), "from": str(r.get("FromGrade") or "").strip(),
                "action": str(r.get("Action") or "").strip().lower(),
                "new": num(r.get("currentPriceTarget")), "old": num(r.get("priorPriceTarget")),
            })
        data["actions"].sort(key=lambda a: a["ts"], reverse=True)
    return data


# ----------------------------- Text card ------------------------------------

def money(v):
    return f"${v:,.2f}"


def tone(value, price):
    diff = (value / price - 1) * 100
    return diff, ("🟢" if diff > 10 else "🔴" if diff < -10 else "🟡")


def caption(d):
    lines = [f"🎯 <b>${html.escape(d['symbol'])} — {html.escape(d['name'])}</b>",
             f"Joriy narx: <b>{money(d['price'])}</b>"]
    if d["mean"]:
        diff, mark = tone(d["mean"], d["price"])
        lines.append(f"O'rtacha maqsad narx: <b>{money(d['mean'])}</b>  {mark} {diff:+.1f}%")
    if d["rec_key"] in RECOMMENDATION:
        lines.append(f"Tavsiya: <b>{RECOMMENDATION[d['rec_key']]}</b>")
    return "\n".join(lines)


def format_card(d):
    tz = ZoneInfo(LOCAL_TZ)
    price = d["price"]
    blocks = []

    if d["mean"]:
        diff, mark = tone(d["mean"], price)
        who = f" ({d['analysts']} analitik)" if d.get("analysts") else ""
        c = [f"📊 <b>Analitiklar konsensusi{who}</b>",
             f"   Joriy narx: <b>{money(price)}</b>",
             f"   O'rtacha maqsad: <b>{money(d['mean'])}</b>  {mark} {diff:+.1f}%"]
        if d["median"]:
            c.append(f"   Median: {money(d['median'])}")
        if d["low"] and d["high"]:
            c.append(f"   Oraliq: {money(d['low'])} – {money(d['high'])}")
        if d["rec_key"] in RECOMMENDATION:
            score = f" ({d['rec_mean']:.1f} / 5)" if d.get("rec_mean") else ""
            c.append(f"   Tavsiya: <b>{RECOMMENDATION[d['rec_key']]}</b>{score}")
        blocks.append("\n".join(c))

    r = d.get("ratings")
    if r and sum(r.values()):
        total = sum(r.values())
        bar = lambda n: "█" * max(1 if n else 0, round(n / total * 12))
        blocks.append("\n".join([
            "🗳 <b>Reytinglar taqsimoti</b>",
            f"<code>Kuchli sotib olish {r['strongBuy']:>3} {bar(r['strongBuy'])}</code>",
            f"<code>Sotib olish        {r['buy']:>3} {bar(r['buy'])}</code>",
            f"<code>Ushlab turish      {r['hold']:>3} {bar(r['hold'])}</code>",
            f"<code>Sotish             {r['sell']:>3} {bar(r['sell'])}</code>",
            f"<code>Kuchli sotish      {r['strongSell']:>3} {bar(r['strongSell'])}</code>",
        ]))

    acts = d["actions"][:MAX_ACTIONS]
    if acts:
        lines = [f"🏦 <b>So'nggi analitik harakatlari</b> <i>(oxirgi {DAYS} kun)</i>"]
        for i, a in enumerate(acts, 1):
            day = datetime.fromtimestamp(a["ts"], tz).strftime("%d.%m.%Y")
            act = ACTION.get(a["action"], a["action"] or "yangiladi")
            grade = (f"{html.escape(a['from'])} → {html.escape(a['to'])}"
                     if a["action"] in ("up", "down") and a["from"] else html.escape(a["to"]))
            lines.append(f"{i}. <b>{html.escape(a['firm'])}</b> · <i>{day}</i>")
            lines.append(f"    {grade} — {act}" if grade else f"    {act}")
            if a["new"] and a["old"] and a["old"] != a["new"]:
                ch = (a["new"] / a["old"] - 1) * 100
                arrow = "🔼" if ch > 0 else "🔽"
                lines.append(f"    🎯 {money(a['old'])} → <b>{money(a['new'])}</b>  {arrow} {ch:+.1f}%")
            elif a["new"]:
                same = " (o'zgarmadi)" if a["old"] == a["new"] else ""
                lines.append(f"    🎯 <b>{money(a['new'])}</b>{same}")
            lines.append("")
        blocks.append("\n".join(lines).strip())
    else:
        blocks.append(f"🏦 <i>Oxirgi {DAYS} kunda analitik harakatlari bo'lmagan.</i>")

    blocks.append(f"<i>{html.escape(DISCLAIMER)}</i>")
    return "\n\n".join(blocks)


# ----------------------------- Chart ----------------------------------------

THEME = {
    "bg": "#0b1220", "panel": "#111a2e", "line": "#1e2a44", "ink": "#e6edf7",
    "muted": "#8a99b8", "up": "#22c55e", "down": "#ef4444", "fair": "#f5b301",
    "neutral": "#eab308",
}


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


def make_chart(d):
    """Dark card: consensus range on top, then the latest target changes (old -> new) per firm."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    T = THEME
    plt.rcParams["font.family"] = _font()
    price, mean = d["price"], d["mean"]
    tz = ZoneInfo(LOCAL_TZ)
    rows = []
    if d["low"] and d["high"] and mean:
        rows.append({"label": "Konsensus", "sub": f"{d['analysts'] or ''} analitik".strip(),
                     "lo": d["low"], "hi": d["high"], "dot": mean, "old": None, "kind": "consensus"})
    for a in [a for a in d["actions"] if a["new"]][:CHART_ACTIONS]:
        rows.append({"label": a["firm"][:22], "sub": datetime.fromtimestamp(a["ts"], tz).strftime("%d.%m"),
                     "lo": None, "hi": None, "dot": a["new"], "old": a["old"], "kind": "action"})
    if not rows:
        return None

    n = len(rows)
    fig_h = 4.6 + 0.62 * n
    fig = plt.figure(figsize=(10, fig_h), dpi=160, facecolor=T["bg"])

    # ---------- header ----------
    head = fig.add_axes([0, 1 - 2.25 / fig_h, 1, 2.25 / fig_h])
    head.set_axis_off()
    head.set_xlim(0, 1)
    head.set_ylim(0, 1)
    head.text(0.04, 0.78, f"${d['symbol']}", fontsize=30, fontweight="bold", color=T["ink"], va="center")
    head.text(0.04, 0.50, d["name"][:46], fontsize=13, color=T["ink"], va="center")
    head.text(0.04, 0.29, f"{datetime.now(timezone.utc):%d.%m.%Y} · Analitiklar maqsad narxlari",
              fontsize=10, color=T["muted"], va="center")

    def stat(x, label, value, color):
        head.text(x, 0.80, label, fontsize=10, color=T["muted"], va="center", ha="left")
        head.text(x, 0.55, value, fontsize=22, fontweight="bold", color=color, va="center", ha="left")

    stat(0.52, "Joriy narx", f"${price:,.2f}", T["ink"])
    if mean:
        diff = (mean / price - 1) * 100
        color = T["up"] if diff > 10 else T["down"] if diff < -10 else T["neutral"]
        stat(0.74, "O'rtacha maqsad", f"${mean:,.2f}", T["fair"])
        word = "O'SISH" if diff >= 0 else "TUSHISH"
        head.text(0.74, 0.25, f" {word}  {diff:+.1f}% ", fontsize=11, fontweight="bold", color=color,
                  ha="left", va="center",
                  bbox=dict(boxstyle="round,pad=0.45,rounding_size=0.8", facecolor=T["bg"],
                            edgecolor=color, linewidth=1.3))
    head.plot([0.04, 0.96], [0.04, 0.04], color=T["line"], linewidth=1, transform=head.transAxes)

    # ---------- rows ----------
    top = 1 - 2.45 / fig_h
    bottom = 0.95 / fig_h
    ax = fig.add_axes([0.25, bottom, 0.53, top - bottom], facecolor=T["bg"])
    pts = [price] + [v for r in rows for v in (r["lo"], r["hi"], r["dot"], r["old"]) if v]
    pad = (max(pts) - min(pts)) * 0.15 or max(pts) * 0.2
    x0, x1 = max(0, min(pts) - pad), max(pts) + pad
    ax.set_xlim(x0, x1)
    ax.set_ylim(-0.7, n - 0.3)

    for i, r in enumerate(rows):
        yy = n - 1 - i
        if i % 2 == 0:
            ax.axhspan(yy - 0.5, yy + 0.5, xmin=-0.42, xmax=1.34, color=T["panel"], zorder=0,
                       linewidth=0, clip_on=False)
        row_y = ax.transData.transform((0, yy))[1] / fig.dpi / fig_h
        if r["kind"] == "consensus":
            ax.barh(yy, r["hi"] - r["lo"], left=r["lo"], height=0.42, color=T["fair"], alpha=0.25,
                    zorder=2, linewidth=0)
            ax.scatter([r["dot"]], [yy], s=130, color=T["fair"], edgecolor=T["bg"], linewidth=2, zorder=5)
            color = T["fair"]
            sub = f"\\${r['lo']:,.0f}–\\${r['hi']:,.0f}"
        else:
            color = T["up"] if r["dot"] >= price else T["down"]
            if r["old"] and r["old"] != r["dot"]:
                ax.annotate("", xy=(r["dot"], yy), xytext=(r["old"], yy), zorder=4,
                            arrowprops=dict(arrowstyle="-|>", color=color, lw=1.8, alpha=0.85,
                                            shrinkA=0, shrinkB=7))
                ax.scatter([r["old"]], [yy], s=40, color=T["muted"], zorder=5, linewidth=0)
                ch = (r["dot"] / r["old"] - 1) * 100
                sub = f"\\${r['old']:,.0f} → {ch:+.1f}%"
            elif r["old"] == r["dot"]:
                sub = "o'zgarmadi"
            else:
                sub = "yangi maqsad"
            ax.scatter([r["dot"]], [yy], s=120, color=color, edgecolor=T["bg"], linewidth=2, zorder=6)
        fig.text(0.04, row_y + 0.06 / fig_h, r["label"], fontsize=11, color=T["ink"], va="center",
                 fontweight="bold" if r["kind"] == "consensus" else "normal")
        fig.text(0.04, row_y - 0.17 / fig_h, r["sub"], fontsize=8.5, color=T["muted"], va="center")
        fig.text(0.955, row_y + 0.06 / fig_h, f"${r['dot']:,.2f}", fontsize=12, fontweight="bold",
                 color=T["ink"], ha="right", va="center")
        fig.text(0.955, row_y - 0.17 / fig_h, sub, fontsize=8.5, color=color, ha="right", va="center")

    ax.axvline(price, color=T["ink"], linestyle=(0, (4, 3)), linewidth=1.3, zorder=3)
    ax.text(price, n - 0.3, f"Joriy ${price:,.0f}", fontsize=9, color=T["ink"], ha="center", va="bottom",
            fontweight="bold")
    ax.set_yticks([])
    ax.tick_params(axis="x", colors=T["muted"], labelsize=9, length=0, pad=10)
    ax.xaxis.set_major_formatter(lambda v, _: f"${v:,.0f}")
    ax.grid(axis="x", color=T["line"], linewidth=0.8, zorder=1)
    for side in ax.spines.values():
        side.set_visible(False)

    fig.text(0.04, 0.32 / fig_h, "Sariq: konsensus oralig'i va o'rtacha maqsad · Strelka: eski → yangi maqsad · "
             "Yashil: joriy narxdan yuqori, qizil: past", fontsize=8, color=T["muted"])
    fig.text(0.96, 0.32 / fig_h, "Trading Laboratory · Yahoo Finance", fontsize=8, color=T["muted"], ha="right")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=T["bg"])
    plt.close(fig)
    return buf.getvalue()


# ----------------------------- Main -----------------------------------------

def main():
    symbol = TICKER_IN.upper().lstrip("$").replace(".", "-")
    if not re.fullmatch(r"[A-Z][A-Z\-]{0,7}", symbol):
        send_text(f"❌ \"{TICKER_IN}\" — noto'g'ri tiker. Masalan: $NVDA")
        return
    d = collect(symbol)
    if not d:
        send_text(f"❌ ${symbol} topilmadi. Tikerni tekshiring (masalan: $AAPL, $NVDA).")
        return
    if not d["mean"] and not d["actions"]:
        send_text(f"ℹ️ ${symbol} ({d['name']}) bo'yicha analitiklarning maqsad narxlari yo'q. "
                  "Bu aksiyani analitiklar kuzatmaydi.")
        return
    print(f"{symbol}: mean {d['mean']}, actions {len(d['actions'])}")

    png = make_chart(d)
    if png:
        send_photo(png, caption(d))
        send_text(format_card(d), reply=False, html_mode=True)
    else:
        send_text(caption(d) + "\n\n" + format_card(d), html_mode=True)


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
