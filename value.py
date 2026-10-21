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


def send_text(text, reply=True):
    for i, chunk in enumerate(split_text(text)):
        tg("sendMessage", {**where(reply and i == 0), "text": chunk,
                           "link_preview_options": {"is_disabled": True}})


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
- Javob o'zbek tilida (lotin yozuvi), oddiy matn. Markdown belgilarini ishlatma: **, ##, ```, jadval
  chiziqlari yo'q. Emoji ishlatish mumkin.
- Har bir usul uchun 2-4 qator: asosiy taxminlar va natija (bir aksiya uchun, $ da).
- Usul bu kompaniyaga mos kelmasa (masalan, dividend to'lanmasa Gordon modeli, yoki erkin pul oqimi
  manfiy bo'lsa DCF), sababini bir qatorda yoz va unga qiymat berma.
- Oxirida: barcha usullar bo'yicha o'rtacha asl qiymat, oraliq (eng past - eng yuqori), joriy narxga
  nisbatan farq (%) va 2-3 gaplik xulosa.
- Javobning umumiy uzunligi 3000 belgidan oshmasin.
- Yakuniy javobni "YAKUNIY JAVOB:" so'zlari bilan boshla.
- Eng oxirgi qatorda natijalarni mashina o'qishi uchun quyidagi formatda ber (bitta qatorda, haqiqiy JSON):
RESULTS_JSON: {{"methods": [{{"name": "DCF", "value": 123.45}}], "fair_value": 120.0, "low": 95.0, "high": 150.0}}
  "methods" ga faqat qiymat hisoblangan usullarni kirit, "name" lar qisqa bo'lsin.
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
    m = re.search(r"RESULTS_JSON:\s*(\{.*\})", text, re.S)
    if m:
        raw = m.group(1)
        for end in range(len(raw), 0, -1):                 # tolerate text after the JSON
            if raw[end - 1] != "}":
                continue
            try:
                results = json.loads(raw[:end])
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
            name, value = str(m["name"]).strip()[:28], float(m["value"])
        except (KeyError, TypeError, ValueError):
            continue
        if name and 0 < value < price * 50:
            methods.append((name, value))
    return methods


# ----------------------------- Chart ----------------------------------------

def make_chart(symbol, name, price, methods, fair):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    labels = [m[0] for m in methods] + (["O'rtacha asl qiymat"] if fair else [])
    values = [m[1] for m in methods] + ([fair] if fair else [])
    good, bad, ink, grid = "#2e9e5b", "#d1495b", "#1f2328", "#d8dee4"
    colors = [good if v >= price else bad for v in values]

    height = 1.6 + 0.55 * len(values)
    fig, ax = plt.subplots(figsize=(8, height), dpi=150)
    y = list(range(len(values)))[::-1]
    bars = ax.barh(y, values, color=colors, height=0.6, zorder=2)
    if fair:
        bars[-1].set_edgecolor(ink)
        bars[-1].set_linewidth(1.5)
    ax.axvline(price, color=ink, linestyle="--", linewidth=1.3, zorder=1)   # behind the bars
    ax.text(price, len(values) - 0.35, f"  Joriy narx ${price:,.2f}", color=ink, fontsize=9,
            va="bottom", ha="left")
    xmax = max(values + [price]) * 1.25
    for yi, v in zip(y, values):
        diff = (v / price - 1) * 100
        label = f"${v:,.2f} ({diff:+.0f}%)"
        if v > xmax * 0.33:          # inside the bar, so the price line never crosses it
            ax.text(v - xmax * 0.012, yi, label, va="center", ha="right", fontsize=9,
                    color="white", fontweight="bold", zorder=3)
        else:
            ax.text(v + xmax * 0.012, yi, label, va="center", ha="left", fontsize=9, color=ink)
    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=10)
    ax.set_xlim(0, xmax)
    ax.xaxis.set_major_formatter(lambda x, _: f"${x:,.0f}")
    ax.grid(axis="x", color=grid, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.set_title(f"${symbol} — {name}\nAsl qiymat (Intrinsic Value) va joriy narx",
                 loc="left", fontsize=12, fontweight="bold", color=ink)
    fig.text(0.01, 0.01, f"Yashil: joriy narxdan yuqori · Qizil: past · {datetime.now(timezone.utc):%Y-%m-%d} · "
             "Investitsion maslahat emas", fontsize=7.5, color="#57606a")
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
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
    if not text:
        raise RuntimeError("Gemini bo'sh javob qaytardi")

    price = data["price"]
    methods = clean_results(results, price)
    fair = None
    if isinstance(results, dict):
        fair = num(results.get("fair_value"))
    if not fair and methods:
        fair = sum(v for _, v in methods) / len(methods)

    header = f"📊 ${symbol} — {name}\n🏛 Birja: {exchange} · Joriy narx: ${price:,.2f}"
    if fair:
        diff = (fair / price - 1) * 100
        verdict = "arzon baholangan 🟢" if diff > 10 else "qimmat baholangan 🔴" if diff < -10 else "adolatli narxga yaqin 🟡"
        header += f"\n⚖️ O'rtacha asl qiymat: ${fair:,.2f} ({diff:+.1f}%) — {verdict}"

    if methods:
        send_photo(make_chart(symbol, name, price, methods, fair), header)
        send_text(text + "\n\n" + DISCLAIMER, reply=False)
    else:
        send_text(header + "\n\n" + text + "\n\n" + DISCLAIMER)
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
