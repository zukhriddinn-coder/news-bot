"""
Trading Laboratory -> Intrinsic Value bot
A subscriber writes $TICKER in the group topic. Google Apps Script starts this
script (GitHub Actions) with the ticker. It:
  1. finds the stock on Yahoo Finance and its exchange (NYSE or NASDAQ),
  2. collects fresh financial data,
  3. sends the matching prompt (prompt_nyse.txt / prompt_nasdaq.txt) + the data to
     Gemini, which does the math by running Python (code execution),
  4. posts a chart and the analysis back to the topic as a reply.
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

import yfinance as yf

# ---------------------------------------------------------------------------
# SETTINGS (you can edit these)
# ---------------------------------------------------------------------------

GEMINI_MODELS = ["gemini-flash-latest", "gemini-2.5-flash", "gemini-flash-lite-latest",
                 "gemini-2.5-flash-lite"]
GEMINI_RETRIES = 2            # attempts per model
GEMINI_TIMEOUT = 300          # seconds to wait for one Gemini answer
MAX_TEXT = 3900               # Telegram allows 4096 characters per message

NASDAQ_CODES = {"NMS", "NGM", "NCM", "NAS"}          # Nasdaq Global Select / Global / Capital
NYSE_CODES = {"NYQ", "ASE", "PCX", "NYS"}            # NYSE, NYSE American, NYSE Arca

DISCLAIMER = "⚠️ Bu investitsion maslahat emas. Hisob-kitoblar taxminlarga asoslangan."

# ---------------------------------------------------------------------------

BOT_TOKEN = re.sub(r"\s+", "", os.environ.get("VALUE_BOT_TOKEN", ""))
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
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
    for i, chunk in enumerate(split_text(text)):
        payload = {**where(reply and i == 0), "text": chunk, "link_preview_options": {"is_disabled": True}}
        if html_mode:
            payload["parse_mode"] = "HTML"
        tg("sendMessage", payload)


def send_photo(png, caption):
    tg("sendPhoto", {**where(), "caption": caption[:1024]},
       files={"photo": ("value.png", png, "image/png")})


def delete_ack():
    if ACK_ID:
        try:
            tg("deleteMessage", {"chat_id": CHAT_ID, "message_id": int(ACK_ID)})
        except Exception as exc:
            print(f"Could not delete the waiting message: {exc}")


def split_text(text, limit=MAX_TEXT):
    parts, rest = [], text.strip()
    while len(rest) > limit:
        cut = rest.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        parts.append(rest)
    return parts


# ----------------------------- Yahoo Finance --------------------------------

def num(v):
    try:
        v = float(v)
        return None if math.isnan(v) or math.isinf(v) else v
    except (TypeError, ValueError):
        return None


def rows_by_year(df, names, years=4):
    """Picks rows from a yfinance statement: {row name: {year: value}} (newest first)."""
    out = {}
    if df is None or getattr(df, "empty", True):
        return out
    for name in names:
        if name in df.index:
            series = {}
            for col in list(df.columns)[:years]:
                val = num(df.loc[name, col])
                if val is not None:
                    series[str(getattr(col, "year", col))] = round(val)
            if series:
                out[name] = series
    return out


def collect_data(symbol):
    t = yf.Ticker(symbol)
    info = t.info or {}
    price = num(info.get("currentPrice")) or num(info.get("regularMarketPrice"))
    if not price:
        try:
            price = num(t.fast_info["last_price"])
        except Exception:
            price = None
    if not price or not (info.get("longName") or info.get("shortName")):
        return None

    keys = ["longName", "exchange", "sector", "industry", "currency", "marketCap", "sharesOutstanding",
            "beta", "trailingEps", "forwardEps", "trailingPE", "forwardPE", "pegRatio", "priceToBook",
            "bookValue", "dividendRate", "dividendYield", "payoutRatio", "fiveYearAvgDividendYield",
            "totalRevenue", "revenueGrowth", "earningsGrowth", "grossMargins", "operatingMargins",
            "profitMargins", "ebitda", "netIncomeToCommon", "freeCashflow", "operatingCashflow",
            "totalCash", "totalDebt", "debtToEquity", "returnOnEquity", "returnOnAssets",
            "targetMeanPrice", "targetLowPrice", "targetHighPrice", "numberOfAnalystOpinions",
            "fiftyTwoWeekLow", "fiftyTwoWeekHigh"]
    data = {"ticker": symbol, "price": round(price, 2),
            "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "info": {k: info.get(k) for k in keys if info.get(k) not in (None, "", "Infinity")}}

    def safe(label, fn):
        try:
            value = fn()
            if value:
                data[label] = value
        except Exception as exc:
            print(f"Yahoo {label}: {exc}")

    safe("cash_flow", lambda: rows_by_year(t.cashflow, [
        "Free Cash Flow", "Operating Cash Flow", "Capital Expenditure",
        "Cash Dividends Paid", "Repurchase Of Capital Stock"]))
    safe("income", lambda: rows_by_year(t.income_stmt, [
        "Total Revenue", "Operating Income", "EBIT", "Net Income", "Diluted EPS",
        "Interest Expense", "Tax Provision", "Pretax Income", "Tax Rate For Calcs"]))
    safe("balance", lambda: rows_by_year(t.balance_sheet, [
        "Total Debt", "Cash And Cash Equivalents", "Stockholders Equity",
        "Ordinary Shares Number"], years=2))

    def growth():
        g = t.growth_estimates
        if g is None or g.empty:
            return None
        col = "stockTrend" if "stockTrend" in g.columns else g.columns[0]
        return {str(i): round(float(v), 4) for i, v in g[col].items() if num(v) is not None}
    safe("growth_estimates", growth)

    def risk_free():
        tnx = yf.Ticker("^TNX").fast_info["last_price"]
        return round(float(tnx) / 100, 4) if tnx else None   # ^TNX is quoted as 4.25 = 4.25%
    safe("us_10y_treasury_yield", risk_free)
    return data


def exchange_of(code):
    code = (code or "").upper()
    if code in NASDAQ_CODES:
        return "NASDAQ"
    if code in NYSE_CODES:
        return "NYSE"
    return None


# ----------------------------- Gemini ---------------------------------------

FORMAT_RULES = """
Quyida {name} bo'yicha Yahoo Finance'dan {date} holatiga olingan haqiqiy ma'lumotlar (JSON).
Hisob-kitoblarda shu ma'lumotlardan foydalan. Yetishmagan qiymatlar (masalan, bozor risk mukofoti,
uzoq muddatli o'sish) uchun asosli taxmin qil va uni aniq ko'rsat. Valyuta: {currency}.

MA'LUMOTLAR:
{data}

JAVOB TALABLARI:
- Barcha hisob-kitoblarni albatta Python kodini ishga tushirib bajar (Monte-Karlo uchun kamida 10 000 ta
  simulyatsiya, natija qayta chiqishi uchun np.random.seed(42)).
- Usul bu kompaniyaga mos kelmasa (masalan, dividend to'lanmasa Gordon modeli, yoki erkin pul oqimi
  manfiy bo'lsa DCF), unga qiymat berma, "skipped" ro'yxatiga sababi bilan yoz.
- Hisob-kitoblardan keyin yakuniy javob sifatida FAQAT bitta RESULTS_JSON ber, boshqa matn yozma.
  Barcha matnlar o'zbek tilida (lotin yozuvi), qisqa va aniq, markdown belgilarisiz:
RESULTS_JSON: {{
 "methods": [
  {{"name": "DCF Valuation", "value": 123.45, "low": 101.2, "high": 148.9,
    "assumptions": ["Erkin pul oqimi o'sishi: 6% (5 yil)", "WACC: 8.5%", "Terminal o'sish: 2.5%"],
    "note": "Bir gaplik izoh: natija nimani anglatadi."}}
 ],
 "skipped": [{{"name": "Gordon Growth Model", "reason": "Kompaniya dividend to'lamaydi"}}],
 "fair_value": 120.0, "low": 95.0, "high": 150.0,
 "summary": "2-3 gaplik professional xulosa: asl qiymat joriy narxga nisbatan qanday va nima uchun."
}}
  "name" — usulning to'liq nomi (ko'pi bilan 24 belgi). "assumptions" — 2-4 ta eng muhim taxmin, har biri
  "Nomi: qiymati" ko'rinishida. "low"/"high" — shu usulning pessimistik va optimistik bahosi (masalan, DCF
  uchun diskont stavkasi va o'sish ±1-2% o'zgarganda; Monte-Karlo va ehtimollik usullari uchun 10- va
  90-persentil). "fair_value" — qiymat hisoblangan usullarning o'rtachasi.
"""


def build_prompt(exchange, symbol, data):
    path = "prompt_nasdaq.txt" if exchange == "NASDAQ" else "prompt_nyse.txt"
    with open(path, encoding="utf-8") as f:
        template = f.read().strip()
    name = data["info"].get("longName") or symbol
    text = template.replace("$XXX", f"${symbol} ({name})", 1).replace("$XXX", f"${data['price']:,.2f}", 1)
    return text + "\n" + FORMAT_RULES.format(
        name=f"${symbol} ({name})", date=data["as_of"], currency=data["info"].get("currency", "USD"),
        data=json.dumps(data, ensure_ascii=False, indent=1))


def gemini(prompt):
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "tools": [{"code_execution": {}}],
        "generationConfig": {"temperature": 0.3},
    }
    body = json.dumps(payload).encode("utf-8")
    last_error = "Gemini javob bermadi"
    for model in GEMINI_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(GEMINI_RETRIES):
            req = urllib.request.Request(url, data=body, headers={
                "Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY})
            try:
                with urllib.request.urlopen(req, timeout=GEMINI_TIMEOUT) as resp:
                    res = json.load(resp)
                parts = res["candidates"][0]["content"]["parts"]
                runs = sum(1 for p in parts if "codeExecutionResult" in p)
                print(f"Gemini {model}: {len(parts)} parts, Python runs: {runs}")
                return parts, model
            except urllib.error.HTTPError as e:
                msg = e.read().decode("utf-8", "replace")[:300]
                print(f"Gemini {model}: HTTP {e.code} {msg}")
                last_error = f"Gemini HTTP {e.code}"
                if e.code in (401, 403) or "API key" in msg:
                    raise RuntimeError("Gemini kaliti noto'g'ri (GEMINI_API_KEY)")
                if e.code == 404 or (e.code == 429 and "PerDay" in msg):
                    break                                  # try the next model
                time.sleep(20 * (attempt + 1))
            except Exception as exc:
                print(f"Gemini {model}: {exc}")
                last_error = str(exc)
                time.sleep(10)
    raise RuntimeError(last_error)


def parse_answer(parts):
    """Returns (analysis text, results dict or None)."""
    text = "\n".join(p.get("text", "") for p in parts if p.get("text"))
    results = None
    m = (re.search(r"RESULTS_JSON:\s*(?:```(?:json)?\s*)?(\{.*\})", text, re.S)
         or re.search(r"```(?:json)?\s*(\{\s*\"methods\".*\})\s*```", text, re.S))
    if m:
        raw = m.group(1)
        for end in range(len(raw), 0, -1):                 # tolerate text after the JSON
            if raw[end - 1] != "}":
                continue
            chunk = raw[:end]
            try:
                results = json.loads(chunk)
                break
            except json.JSONDecodeError:
                try:                                        # forgive trailing commas
                    results = json.loads(re.sub(r",\s*([}\]])", r"\1", chunk))
                    break
                except json.JSONDecodeError:
                    continue
        text = text[:m.start()]
    if "YAKUNIY JAVOB:" in text:
        text = text[text.rindex("YAKUNIY JAVOB:") + len("YAKUNIY JAVOB:"):]
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = text.replace("**", "").replace("__", "")
    text = re.sub(r"^#+\s*", "", text, flags=re.M)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text, results


def clean_results(results, price):
    if not isinstance(results, dict):
        return []
    methods = []
    for m in results.get("methods") or []:
        try:
            name, value = str(m["name"]).strip()[:24], float(m["value"])
        except (KeyError, TypeError, ValueError):
            continue
        lo, hi = num(m.get("low")), num(m.get("high"))
        if not (lo and hi and 0 < lo <= value <= hi < price * 50):
            lo = hi = None
        if name and 0 < value < price * 50:
            methods.append((name, value, lo, hi))
    return methods



# ----------------------------- Text card ------------------------------------

NUMBERS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣"]


def esc(s):
    return html.escape(str(s or "").strip().replace("**", ""), quote=False)


def tone(value, price):
    diff = (value / price - 1) * 100
    mark = "🟢" if diff > 10 else "🔴" if diff < -10 else "🟡"
    return diff, mark


def money(v):
    return f"${v:,.2f}"


def format_card(symbol, price, results):
    """Builds the analysis message (Telegram HTML) from Gemini's structured result.
    Returns None when the result has no usable methods."""
    if not isinstance(results, dict):
        return None
    blocks = [f"📋 <b>${esc(symbol)} — tahlil tafsilotlari</b>"]
    n = 0
    values, ranges = [], []
    for m in results.get("methods") or []:
        value = num(m.get("value")) if isinstance(m, dict) else None
        if not value or value <= 0 or value > price * 50:
            continue
        diff, mark = tone(value, price)
        lo, hi = num(m.get("low")), num(m.get("high"))
        lines = [f"{NUMBERS[n] if n < len(NUMBERS) else '▪️'} <b>{esc(m.get('name'))}</b>"]
        for a in (m.get("assumptions") or [])[:4]:
            lines.append(f"   ▫️ {esc(a)}")
        result = f"   💵 <b>{money(value)}</b>  {mark} {diff:+.1f}%"
        if lo and hi and lo <= value <= hi:
            result += f"\n   ↔️ <i>Oraliq: {money(lo)} – {money(hi)}</i>"
        lines.append(result)
        if m.get("note"):
            lines.append(f"   <i>{esc(m.get('note'))}</i>")
        blocks.append("\n".join(lines))
        values.append(value)
        ranges.extend([lo or value, hi or value])
        n += 1
    if not values:
        return None

    skipped = [s for s in (results.get("skipped") or []) if isinstance(s, dict) and s.get("name")]
    if skipped:
        blocks.append("\n".join(["⛔️ <b>Qo'llanilmagan usullar</b>"] +
                                [f"   ▫️ {esc(s['name'])}: <i>{esc(s.get('reason'))}</i>" for s in skipped]))

    fair = num(results.get("fair_value")) or sum(values) / len(values)
    diff, mark = tone(fair, price)
    verdict = "arzon baholangan" if diff > 10 else "qimmat baholangan" if diff < -10 else "adolatli narxga yaqin"
    lo, hi = num(results.get("low")) or min(ranges), num(results.get("high")) or max(ranges)
    summary = [
        "━━━━━━━━━━━━━━━",
        "⚖️ <b>Xulosa</b>",
        f"   Joriy narx: <b>{money(price)}</b>",
        f"   O'rtacha asl qiymat: <b>{money(fair)}</b>  {mark} {diff:+.1f}%",
    ]
    if hi - lo >= 0.01:
        summary.append(f"   Baholash oralig'i: {money(lo)} – {money(hi)}")
    summary.append(f"   Holat: <b>{verdict}</b>")
    if results.get("summary"):
        summary.append(f"<blockquote>{esc(results['summary'])}</blockquote>")
    blocks.append("\n".join(summary))
    blocks.append(f"<i>{esc(DISCLAIMER)}</i>")
    return "\n\n".join(blocks)


# ----------------------------- Chart ----------------------------------------

THEME = {
    "bg": "#0b1220", "panel": "#111a2e", "line": "#1e2a44", "ink": "#e6edf7",
    "muted": "#8a99b8", "up": "#22c55e", "down": "#ef4444", "fair": "#f5b301",
    "neutral": "#eab308",
}


def _font():
    from matplotlib import font_manager
    names = {f.name for f in font_manager.fontManager.ttflist}
    return "Inter" if "Inter" in names else "DejaVu Sans"


def make_chart(symbol, name, price, methods, fair, exchange="", low=None, high=None):
    """Draws a dark 'football field' valuation card. methods = [(name, value, low, high)]."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    T = THEME
    plt.rcParams["font.family"] = _font()
    n = len(methods)
    fig_h = 4.6 + 0.62 * n
    fig = plt.figure(figsize=(10, fig_h), dpi=160, facecolor=T["bg"])

    # ---------- header ----------
    head = fig.add_axes([0, 1 - 2.25 / fig_h, 1, 2.25 / fig_h])
    head.set_axis_off()
    head.set_xlim(0, 1)
    head.set_ylim(0, 1)
    head.text(0.04, 0.78, f"${symbol}", fontsize=30, fontweight="bold", color=T["ink"], va="center")
    head.text(0.04, 0.50, name[:46], fontsize=13, color=T["ink"], va="center")
    head.text(0.04, 0.29, f"{exchange} · {datetime.now(timezone.utc):%d.%m.%Y} · Intrinsic Value",
              fontsize=10, color=T["muted"], va="center")

    def stat(x, label, value, color):
        head.text(x, 0.80, label, fontsize=10, color=T["muted"], va="center", ha="left")
        head.text(x, 0.55, value, fontsize=22, fontweight="bold", color=color, va="center", ha="left")

    stat(0.52, "Joriy narx", f"${price:,.2f}", T["ink"])
    if fair:
        diff = (fair / price - 1) * 100
        tone = T["up"] if diff > 10 else T["down"] if diff < -10 else T["neutral"]
        verdict = "ARZON" if diff > 10 else "QIMMAT" if diff < -10 else "ADOLATLI"
        stat(0.74, "Asl qiymat", f"${fair:,.2f}", T["fair"])
        head.text(0.74, 0.25, f" {verdict}  {diff:+.1f}% ", fontsize=11, fontweight="bold", color=tone,
                  ha="left", va="center",
                  bbox=dict(boxstyle="round,pad=0.45,rounding_size=0.8", facecolor=T["bg"],
                            edgecolor=tone, linewidth=1.3))
    head.plot([0.04, 0.96], [0.04, 0.04], color=T["line"], linewidth=1, transform=head.transAxes)

    # ---------- football field ----------
    top = 1 - 2.45 / fig_h
    bottom = 0.95 / fig_h
    ax = fig.add_axes([0.25, bottom, 0.53, top - bottom], facecolor=T["bg"])
    lows = [m[2] if m[2] else m[1] for m in methods]
    highs = [m[3] if m[3] else m[1] for m in methods]
    span_lo = min(lows + [price] + ([fair] if fair else []))
    span_hi = max(highs + [price] + ([fair] if fair else []))
    pad = (span_hi - span_lo) * 0.18 or span_hi * 0.2
    x0, x1 = max(0, span_lo - pad), span_hi + pad
    ax.set_xlim(x0, x1)
    ax.set_ylim(-0.7, n - 0.3)

    for i, (label, value, lo, hi) in enumerate(methods):
        yy = n - 1 - i
        color = T["up"] if value >= price else T["down"]
        if i % 2 == 0:
            ax.axhspan(yy - 0.5, yy + 0.5, xmin=-0.42, xmax=1.34, color=T["panel"], zorder=0,
                       linewidth=0, clip_on=False)
        if lo and hi and hi > lo:
            ax.barh(yy, hi - lo, left=lo, height=0.42, color=color, alpha=0.30, zorder=2, linewidth=0)
        ax.scatter([value], [yy], s=120, color=color, edgecolor=T["bg"], linewidth=2, zorder=5)
        row_y = ax.transData.transform((0, yy))[1] / fig.dpi / fig_h
        diff = (value / price - 1) * 100
        fig.text(0.955, row_y + 0.06 / fig_h, f"${value:,.2f}", fontsize=12, fontweight="bold",
                 color=T["ink"], ha="right", va="center")
        sub = f"{diff:+.1f}%" + (f"  ·  \\${lo:,.0f}–\\${hi:,.0f}" if lo and hi and hi > lo else "")
        fig.text(0.955, row_y - 0.17 / fig_h, sub, fontsize=8.5, color=color, ha="right", va="center")
        fig.text(0.04, row_y, label, fontsize=11, color=T["ink"], va="center", ha="left")

    ax.axvline(price, color=T["ink"], linestyle=(0, (4, 3)), linewidth=1.3, zorder=3)
    ax.text(price, n - 0.3, f"Joriy ${price:,.0f}", fontsize=9, color=T["ink"], ha="center", va="bottom",
            fontweight="bold")
    if fair:
        ax.axvline(fair, color=T["fair"], linewidth=1.6, zorder=3)
        ax.text(fair, -0.7, f"Asl qiymat ${fair:,.0f}", fontsize=9, color=T["fair"], ha="center",
                va="top", fontweight="bold")

    ax.set_yticks([])
    ax.tick_params(axis="x", colors=T["muted"], labelsize=9, length=0, pad=18)
    ax.xaxis.set_major_formatter(lambda v, _: f"${v:,.0f}")
    ax.grid(axis="x", color=T["line"], linewidth=0.8, zorder=1)
    for side in ax.spines.values():
        side.set_visible(False)

    fig.text(0.04, 0.32 / fig_h, "Nuqta: usul bo'yicha asl qiymat · Chiziq: pessimistik–optimistik oraliq · "
             "Yashil: joriy narxdan yuqori, qizil: past", fontsize=8, color=T["muted"])
    fig.text(0.96, 0.32 / fig_h, "Trading Laboratory · Investitsion maslahat emas", fontsize=8,
             color=T["muted"], ha="right")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=T["bg"])
    plt.close(fig)
    return buf.getvalue()


# ----------------------------- Main -----------------------------------------

def main():
    symbol = TICKER_IN.upper().lstrip("$").replace(".", "-")
    if not re.fullmatch(r"[A-Z][A-Z\-]{0,7}", symbol):
        send_text(f"❌ \"{TICKER_IN}\" — noto'g'ri tiker. Masalan: $AAPL")
        delete_ack()
        return

    try:
        data = collect_data(symbol)
    except Exception as exc:
        print(f"Yahoo error: {exc}")
        data = None
    if not data:
        send_text(f"❌ ${symbol} topilmadi. Tikerni tekshiring (masalan: $AAPL, $KO).")
        delete_ack()
        return

    exchange = exchange_of(data["info"].get("exchange"))
    name = data["info"].get("longName") or symbol
    if not exchange:
        send_text(f"ℹ️ ${symbol} ({name}) NYSE yoki NASDAQ birjasida emas "
                  f"(birja kodi: {data['info'].get('exchange', '?')}). Hozircha faqat shu ikki birja qo'llab-quvvatlanadi.")
        delete_ack()
        return
    print(f"{symbol}: {name}, {exchange}, price {data['price']}")

    parts, model = gemini(build_prompt(exchange, symbol, data))
    text, results = parse_answer(parts)
    if not text and not results:
        raise RuntimeError("Gemini bo'sh javob qaytardi")

    price = data["price"]
    methods = clean_results(results, price)
    fair = None
    if isinstance(results, dict):
        fair = num(results.get("fair_value"))
    if not fair and methods:
        fair = sum(m[1] for m in methods) / len(methods)

    header = f"📊 ${symbol} — {name}\n🏛 Birja: {exchange} · Joriy narx: ${price:,.2f}"
    if fair:
        diff = (fair / price - 1) * 100
        verdict = "arzon baholangan 🟢" if diff > 10 else "qimmat baholangan 🔴" if diff < -10 else "adolatli narxga yaqin 🟡"
        header += f"\n⚖️ O'rtacha asl qiymat: ${fair:,.2f} ({diff:+.1f}%) — {verdict}"

    card = format_card(symbol, price, results)
    if methods:
        send_photo(make_chart(symbol, name, price, methods, fair, exchange), header)
        if card:
            send_text(card, reply=False, html_mode=True)
        else:
            send_text(text + "\n\n" + DISCLAIMER, reply=False)
    else:
        send_text(header + "\n\n" + (text or "Natija olinmadi.") + "\n\n" + DISCLAIMER)
    delete_ack()
    print(f"Done with {model}, methods on chart: {len(methods)}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}")
        try:
            send_text(f"❌ ${TICKER_IN.upper().lstrip('$')} bo'yicha tahlil qilib bo'lmadi: {exc}\n"
                      "Birozdan keyin qayta urinib ko'ring.")
            delete_ack()
        finally:
            raise SystemExit(1)
