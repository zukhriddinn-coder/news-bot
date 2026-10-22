"""
Trading Laboratory -> which superinvestors hold a stock
A subscriber writes $TICKER in the Superinvestors topic. Google Apps Script starts this script
(GitHub Actions). It reads superinvestors.json (made daily by superinvestors_build.py from SEC 13F
filings) and replies with a chart card and a text card: who holds the stock, who bought,
added, reduced or sold it in the latest quarter, and how big it is in each portfolio.
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
from datetime import date, datetime, timezone

import yfinance as yf

# ---------------------------------------------------------------------------
# SETTINGS (you can edit these)
# ---------------------------------------------------------------------------

DATA_FILE = "superinvestors.json"
SAME_CHANGE = 0.01            # share change under 1% counts as "o'zgarmadi"
DISCLAIMER = ("⚠️ Investitsion maslahat emas. Manba: SEC 13F hisobotlari. 13F chorak oxiridagi holatni "
              "ko'rsatadi va 45 kungacha kechikib topshiriladi; faqat AQSh aksiyalaridagi uzun pozitsiyalar "
              "va optsionlar kiradi.")

# ---------------------------------------------------------------------------

BOT_TOKEN = re.sub(r"\s+", "", os.environ.get("SUPER_BOT_TOKEN", ""))
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
    limit = 3900
    parts, rest = [], text
    while len(rest) > limit:                      # split long answers on blank lines
        cut = rest.rfind("\n\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        parts.append(rest[:cut])
        rest = rest[cut:].lstrip()
    parts.append(rest)
    for i, chunk in enumerate(parts):
        p = {**where(reply and i == 0), "text": chunk, "link_preview_options": {"is_disabled": True}}
        if html_mode:
            p["parse_mode"] = "HTML"
        tg("sendMessage", p)


def send_photo(png, caption):
    tg("sendPhoto", {**where(), "caption": caption[:1024], "parse_mode": "HTML"},
       files={"photo": ("superinvestors.png", png, "image/png")})


def delete_ack():
    if ACK_ID:
        try:
            tg("deleteMessage", {"chat_id": CHAT_ID, "message_id": int(ACK_ID)})
        except Exception as exc:
            print(f"Could not delete the waiting message: {exc}")


# ----------------------------- Helpers --------------------------------------

def big(v):
    a = abs(v)
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            return f"{v / div:,.1f}{suf}"
    return f"{v:,.0f}"


def esc(s):
    return html.escape(str(s), quote=False)


def quarter(report_date):
    d = date.fromisoformat(report_date)
    return f"Q{(d.month - 1) // 3 + 1} {d.year}"


NAME_NOISE = re.compile(r"\b(INC|INCORPORATED|CORP|CORPORATION|CO|COMPANY|LTD|LIMITED|PLC|HOLDINGS?|GROUP|"
                        r"NV|SA|AG|SE|LP|LLC|THE|COM|NEW|CL|CLASS|[A-C])\b")


GENERIC = {"BANK", "FIRST", "AMERICAN", "GENERAL", "UNITED", "NATIONAL", "INTERNATIONAL", "GLOBAL", "CAPITAL",
           "FINANCIAL", "ENERGY", "HEALTH", "DIGITAL", "SOUTHERN", "NORTHERN", "WESTERN", "EASTERN", "PACIFIC",
           "TECHNOLOGIES", "TECHNOLOGY", "SYSTEMS", "THERAPEUTICS", "PHARMACEUTICALS", "RESOURCES", "TRUST"}


def norm_name(s):
    s = (s or "").upper().replace("'", "").replace("\u2019", "")
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    return " ".join(NAME_NOISE.sub(" ", s).split())


def same_company(issuer, name_key):
    """13F issuer names are abbreviated ("OCCIDENTAL PETE" = "Occidental Petroleum"): the first word
    must match exactly (and be distinctive), the second word must start with the same letter."""
    a, b = norm_name(issuer).split(), name_key.split()
    if not a or not b:
        return False
    if a == b:
        return True
    if a[0] != b[0] or len(a[0]) < 4 or a[0] in GENERIC:
        return False
    if len(a) > 1 and len(b) > 1:
        return a[1][0] == b[1][0]
    return False            # "APPLE HOSPITALITY" is not "APPLE"


# ----------------------------- Analysis -------------------------------------

def find_positions(snapshot, symbol, name_key):
    """Positions of this stock in one 13F: by ticker, or by issuer name when the ticker is unknown."""
    if not snapshot:
        return {}
    out = {}
    for h in snapshot["holdings"].values():
        t = (h.get("ticker") or "").upper()
        hit = t == symbol if t else bool(name_key) and same_company(h.get("issuer"), name_key)
        if hit:
            out[h["kind"]] = h
    return out


def analyse(data, symbol, name_key):
    rows = []
    for m in data["managers"]:
        now = find_positions(m["latest"], symbol, name_key)
        before = find_positions(m.get("previous"), symbol, name_key)
        if not now and not before:
            continue
        total = m["latest"].get("total_value") or 0
        sh_now, sh_before = now.get("SH"), before.get("SH")
        row = {"person": m["person"], "fund": m["fund"], "report": m["latest"]["report_date"],
               "filed": m["latest"]["filed"], "prev_report": (m.get("previous") or {}).get("report_date"),
               "shares": sh_now["shares"] if sh_now else 0, "value": sh_now["value"] if sh_now else 0,
               "weight": (sh_now["value"] / total) if sh_now and total else 0,
               "options": {k: v for k, v in now.items() if k != "SH"}}
        if sh_now and total:
            ranks = sorted((h["value"] for h in m["latest"]["holdings"].values() if h["kind"] == "SH"),
                           reverse=True)
            row["rank"] = ranks.index(sh_now["value"]) + 1
            row["positions"] = len(ranks)
        if sh_now and not sh_before:
            row["status"], row["change"] = ("new" if m.get("previous") else "hold"), None
        elif sh_before and not sh_now:
            row["status"], row["change"] = "sold", -1.0
            row["sold_shares"] = sh_before["shares"]
        elif sh_now and sh_before:
            ch = sh_now["shares"] / sh_before["shares"] - 1 if sh_before["shares"] else 0
            row["status"] = "add" if ch > SAME_CHANGE else "reduce" if ch < -SAME_CHANGE else "hold"
            row["change"] = ch
        else:
            row["status"], row["change"] = "options", None          # only puts / calls
        rows.append(row)
    order = {"new": 0, "add": 1, "hold": 2, "reduce": 3, "options": 4, "sold": 5}
    rows.sort(key=lambda r: (r["status"] == "sold", -r["weight"], order[r["status"]]))
    return rows


STATUS = {
    "new": ("🆕", "YANGI pozitsiya"), "add": ("🟢", "ko'paytirdi"), "hold": ("⚪", "o'zgarmadi"),
    "reduce": ("🟠", "kamaytirdi"), "sold": ("🔴", "TO'LIQ SOTDI"), "options": ("🎯", "faqat optsion (PUT = tushishga, CALL = ko'tarilishga tikish)"),
}


def status_text(r):
    icon, word = STATUS[r["status"]]
    if r["status"] in ("add", "reduce") and r["change"] is not None:
        word += f" {r['change'] * 100:+.0f}%"
    return icon, word


# ----------------------------- Text -----------------------------------------

def format_text(symbol, name, rows, data):
    holders = [r for r in rows if r["status"] != "sold"]
    counts = {k: sum(1 for r in rows if r["status"] == k) for k in STATUS}
    lines = [f"🏛 <b>${esc(symbol)} — superinvestorlar</b>",
             f"<i>{esc(name)} · {len(data['managers'])} ta investor kuzatiladi</i>", "",
             f"📊 <b>Umumiy manzara</b>",
             f"   Ushlab turibdi: <b>{len(holders)}</b> ta investor",
             f"   🆕 Yangi: {counts['new']} · 🟢 Ko'paytirdi: {counts['add']} · "
             f"🟠 Kamaytirdi: {counts['reduce']} · 🔴 Sotdi: {counts['sold']}", ""]
    for r in rows:
        icon, word = status_text(r)
        lines.append(f"{icon} <b>{esc(r['person'])}</b> · <i>{esc(r['fund'])}</i>")
        if r["status"] == "sold":
            lines.append(f"    {word} ({big(r.get('sold_shares', 0))} aksiya edi)")
        elif r["shares"]:
            pos = f" · portfelda #{r['rank']} / {r['positions']}" if r.get("rank") else ""
            lines.append(f"    {big(r['shares'])} aksiya · ${big(r['value'])} · "
                         f"portfelning <b>{r['weight'] * 100:.1f}%</b>{pos}")
            lines.append(f"    {word}")
        for kind, o in r["options"].items():
            lines.append(f"    🎯 {kind} optsion: {big(o['shares'])} aksiyaga · ${big(o['value'])}")
        lines.append(f"    <i>{quarter(r['report'])} hisoboti, {datetime.fromisoformat(r['filed']):%d.%m.%Y} "
                     f"da topshirilgan</i>")
        lines.append("")
    updated = datetime.fromisoformat(data["updated"]).strftime("%d.%m.%Y")
    lines.append(f"<i>Ma'lumot yangilangan: {updated}. {esc(DISCLAIMER)}</i>")
    return "\n".join(lines).strip()


# ----------------------------- Chart ----------------------------------------

THEME = {"bg": "#0b1220", "panel": "#111a2e", "line": "#1e2a44", "ink": "#e6edf7", "muted": "#8a99b8",
         "new": "#38bdf8", "add": "#22c55e", "hold": "#94a3b8", "reduce": "#f59e0b", "sold": "#ef4444",
         "options": "#a78bfa", "bar": "#3b82f6"}
PILL = {"new": "YANGI", "add": "KO'PAYTIRDI", "hold": "O'ZGARMADI", "reduce": "KAMAYTIRDI",
        "sold": "SOTDI", "options": "OPTSION"}


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


def make_chart(symbol, name, rows, data):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    T = THEME
    plt.rcParams["font.family"] = _font()
    rows = rows[:14]
    n = len(rows)
    fig_h = 2.9 + 0.56 * n
    fig = plt.figure(figsize=(10, fig_h), dpi=160, facecolor=T["bg"])
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_axis_off()
    ax.set_xlim(0, 1)
    ax.set_ylim(0, fig_h)
    y = fig_h

    holders = sum(1 for r in rows if r["status"] != "sold")
    buys = sum(1 for r in rows if r["status"] in ("new", "add"))
    sells = sum(1 for r in rows if r["status"] in ("reduce", "sold"))
    ax.text(0.04, y - 0.55, f"${symbol}", fontsize=28, fontweight="bold", color=T["ink"], va="center")
    ax.text(0.04, y - 1.03, name[:44], fontsize=12.5, color=T["ink"], va="center")
    ax.text(0.04, y - 1.40, f"Superinvestorlar · SEC 13F · yangilangan "
                            f"{datetime.fromisoformat(data['updated']):%d.%m.%Y}", fontsize=9.5,
            color=T["muted"], va="center")
    for x, label, value, color in ((0.60, "Ushlaydi", str(holders), T["ink"]),
                                   (0.73, "Sotib oldi", str(buys), T["add"]),
                                   (0.86, "Sotdi", str(sells), T["sold"])):
        ax.text(x, y - 0.45, label, fontsize=9.5, color=T["muted"], va="center")
        ax.text(x, y - 0.88, value, fontsize=22, fontweight="bold", color=color, va="center")
    ax.plot([0.04, 0.96], [y - 1.75, y - 1.75], color=T["line"], linewidth=1)
    ax.text(0.04, y - 2.0, "Investor", fontsize=9, color=T["muted"], va="center")
    ax.text(0.42, y - 2.0, "Portfeldagi ulushi", fontsize=9, color=T["muted"], va="center")
    ax.text(0.955, y - 2.0, "So'nggi chorakda", fontsize=9, color=T["muted"], va="center", ha="right")

    max_w = max([r["weight"] for r in rows] + [0.01])
    y0 = y - 2.42
    for k, r in enumerate(rows):
        cy = y0 - k * 0.56
        if k % 2 == 0:
            ax.add_patch(plt.Rectangle((0.03, cy - 0.28), 0.94, 0.56, color=T["panel"], linewidth=0))
        ax.text(0.04, cy + 0.08, r["person"][:26], fontsize=11, color=T["ink"], va="center")
        ax.text(0.04, cy - 0.15, r["fund"][:34], fontsize=8.5, color=T["muted"], va="center")
        if r["weight"]:
            w = 0.26 * r["weight"] / max_w
            ax.add_patch(plt.Rectangle((0.42, cy - 0.09), w, 0.18, color=T["bar"], linewidth=0))
            ax.text(0.42 + w + 0.01, cy, f"{r['weight'] * 100:.1f}%  ·  ${big(r['value'])}", fontsize=9.5,
                    color=T["ink"], va="center")
        elif r["status"] == "sold":
            ax.text(0.42, cy, "pozitsiya yopildi", fontsize=9.5, color=T["muted"], va="center")
        else:
            opt = " + ".join(f"{k} optsion · ${big(o['value'])}" for k, o in r["options"].items())
            ax.text(0.42, cy, opt or "faqat optsion", fontsize=9.5, color=T["ink"], va="center")
        color = T[r["status"]]
        label = PILL[r["status"]]
        if r["status"] == "options":
            label = " + ".join(r["options"]) or label
        if r["status"] in ("add", "reduce") and r["change"] is not None:
            label += f" {r['change'] * 100:+.0f}%"
        ax.text(0.955, cy, f" {label} ", fontsize=9, fontweight="bold", color=color, va="center", ha="right",
                bbox=dict(boxstyle="round,pad=0.35,rounding_size=0.7", facecolor=T["bg"], edgecolor=color,
                          linewidth=1.1))
    ax.text(0.04, 0.32, "13F: chorak oxiridagi holat, 45 kungacha kechikadi", fontsize=8, color=T["muted"],
            va="center")
    ax.text(0.96, 0.32, "Trading Laboratory · Investitsion maslahat emas", fontsize=8, color=T["muted"],
            va="center", ha="right")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=T["bg"])
    plt.close(fig)
    return buf.getvalue()


# ----------------------------- Main -----------------------------------------

def main():
    symbol = TICKER_IN.upper().lstrip("$").replace(".", "-")
    if not re.fullmatch(r"[A-Z][A-Z\-]{0,7}", symbol):
        send_text(f"❌ \"{TICKER_IN}\" — noto'g'ri tiker. Masalan: $AAPL")
        return
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        send_text("⏳ Superinvestorlar ma'lumoti hali tayyorlanmagan. Birozdan keyin qayta urinib ko'ring.")
        return
    try:
        info = yf.Ticker(symbol).info or {}
    except Exception:
        info = {}
    name = info.get("longName") or info.get("shortName") or symbol
    rows = analyse(data, symbol, norm_name(name) if name != symbol else "")
    print(f"{symbol}: {len(rows)} superinvestors")
    if not rows:
        send_text(f"ℹ️ ${symbol} ({name}) — kuzatilayotgan {len(data['managers'])} ta superinvestordan hech biri "
                  "so'nggi ikki chorakda bu aksiyani ushlamagan.")
        return
    holders = sum(1 for r in rows if r["status"] != "sold")
    caption = (f"🏛 <b>${esc(symbol)} — superinvestorlar</b>\n"
               f"{holders} ta investor ushlab turibdi · "
               f"{sum(1 for r in rows if r['status'] in ('new', 'add'))} ta sotib oldi · "
               f"{sum(1 for r in rows if r['status'] in ('reduce', 'sold'))} ta sotdi")
    send_photo(make_chart(symbol, name, rows, data), caption)
    send_text(format_text(symbol, name, rows, data), reply=False, html_mode=True)


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
