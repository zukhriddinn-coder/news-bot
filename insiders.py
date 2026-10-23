"""
Trading Laboratory -> insider trading (SEC Form 4)
A subscriber writes $TICKER in the Insider topic. Google Apps Script starts this script (GitHub Actions).
It downloads the company's Form 4 filings of the last 6 months straight from SEC EDGAR (official, free)
and replies with a chart card and a text card: open-market purchases and sales by executives,
directors and 10% owners, the sums, planned (10b5-1) sales and "cluster" buys (several insiders
buying within a few weeks).

SEC rules: at most 10 requests per second and a User-Agent with a contact e-mail (SEC_CONTACT secret).
"""

import html
import io
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# SETTINGS (you can edit these)
# ---------------------------------------------------------------------------

DAYS = 183                    # look back ~6 months
RECENT_DAYS = 91              # "last 3 months" subtotal
CLUSTER_WINDOW = 30           # cluster = several insiders buying within 30 days ...
CLUSTER_MIN = 2               # ... at least 2 different people (3+ = strong cluster)
MAX_FILINGS = 300             # newest Form 4 filings read per request
DISCLAIMER = ("⚠️ Investitsion maslahat emas. Manba: SEC Form 4. Insayder savdodan keyin 2 ish kuni ichida "
              "hisobot topshiradi. Sotishning sababi ko'p bo'lishi mumkin (soliq, uy, diversifikatsiya); "
              "ochiq bozordagi xarid — kuchliroq signal.")

# ---------------------------------------------------------------------------

BOT_TOKEN = re.sub(r"\s+", "", os.environ.get("INSIDER_BOT_TOKEN", ""))
SEC_CONTACT = os.environ.get("SEC_CONTACT", "").strip()
TICKER_IN = os.environ.get("TICKER", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()
THREAD_ID = os.environ.get("THREAD_ID", "").strip()
REPLY_TO = os.environ.get("REPLY_TO", "").strip()
ACK_ID = os.environ.get("ACK_ID", "").strip()
UA = f"TradingLaboratory research bot {SEC_CONTACT}".strip()

MONTHS = ["Yan", "Fev", "Mar", "Apr", "May", "Iyn", "Iyl", "Avg", "Sen", "Okt", "Noy", "Dek"]
OTHER_CODES = {"M": "optsion ijrosi", "A": "mukofot (grant) aksiyalar", "F": "soliq uchun ushlab qolish",
               "G": "sovg'a", "C": "konvertatsiya", "X": "optsion ijrosi", "D": "kompaniyaga qaytarish",
               "J": "boshqa"}


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
       files={"photo": ("insiders.png", png, "image/png")})


def delete_ack():
    if ACK_ID:
        try:
            tg("deleteMessage", {"chat_id": CHAT_ID, "message_id": int(ACK_ID)})
        except Exception as exc:
            print(f"Could not delete the waiting message: {exc}")


# ----------------------------- SEC ------------------------------------------

_rate_lock = threading.Lock()
_last_sec = [0.0]


def sec_get(url, as_json=True):
    """GET from SEC with the required User-Agent, at most ~8 requests per second (thread safe)."""
    for attempt in range(4):
        with _rate_lock:
            wait = 0.125 - (time.time() - _last_sec[0])
            if wait > 0:
                time.sleep(wait)
            _last_sec[0] = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "identity"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
            return json.loads(data) if as_json else data
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and attempt < 3:
                time.sleep(3 * (attempt + 1))
                continue
            raise
    raise RuntimeError(f"SEC: too many retries for {url}")


def find_company(symbol):
    """Ticker -> (CIK, SEC name) from SEC's official ticker list."""
    tickers = sec_get("https://www.sec.gov/files/company_tickers.json")
    want = {symbol, symbol.replace("-", "."), symbol.replace("-", "")}
    for row in tickers.values():
        if str(row.get("ticker", "")).upper() in want:
            return int(row["cik_str"]), row.get("title", symbol)
    return None, None


def form4_list(cik, since):
    sub = sec_get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
    r = sub["filings"]["recent"]
    out = []
    for form, acc, filed, doc in zip(r["form"], r["accessionNumber"], r["filingDate"], r["primaryDocument"]):
        if form == "4" and filed >= since and doc.lower().endswith(".xml"):
            out.append({"accession": acc, "filed": filed, "doc": doc.split("/")[-1]})   # drop xslF345X05/
    out.sort(key=lambda f: f["filed"], reverse=True)
    return sub.get("name", ""), out[:MAX_FILINGS]


# ----------------------------- Form 4 parsing -------------------------------

def local(tag):
    return tag.rsplit("}", 1)[-1]


def child(el, name):
    if el is None:
        return None
    for c in el:
        if local(c.tag) == name:
            return c
    return None


def val(el, *path):
    """Text of el/path[0]/path[1]..., also looking into a trailing <value> element."""
    for name in path:
        el = child(el, name)
    if el is None:
        return ""
    v = child(el, "value")
    return ((v.text if v is not None else el.text) or "").strip()


def num(s):
    try:
        return float(str(s).replace(",", ""))
    except ValueError:
        return 0.0


def truthy(s):
    return str(s).strip().lower() in ("1", "true")


ENTITY = re.compile(r"\b(INC|LLC|L\.?P|LTD|TRUST|FUND|CAPITAL|HOLDINGS?|PARTNERS|CORP|GROUP|MANAGEMENT|"
                    r"FOUNDATION|ADVISORS?|INVESTMENTS?|COMPANY|CO|PLC|AG|SA|NV)\b\.?", re.I)


def nice_name(raw):
    """SEC writes people as 'COOK TIMOTHY D' -> 'Timothy D Cook'. Companies stay as they are."""
    raw = re.sub(r"\s+", " ", raw).strip()
    if ENTITY.search(raw):
        out = raw.title() if raw.isupper() else raw
        return re.sub(r"\b(Llc|Lp|L\.P\.|Inc|Ltd|Plc|Ag|Sa|Nv)\b",
                      lambda m: m.group(0).upper() if m.group(0) != "Inc" else "Inc", out)
    parts = raw.replace(",", " ").split()
    if 2 <= len(parts) <= 4:
        parts = parts[1:] + parts[:1]
    out = " ".join(parts)
    return out.title() if out.isupper() else out


TITLES = [(r"chief executive|\bceo\b", "CEO"), (r"chief financial|\bcfo\b", "CFO"),
          (r"chief operating|\bcoo\b", "COO"), (r"chief technology|\bcto\b", "CTO"),
          (r"chief accounting|\bcao\b", "CAO"), (r"general counsel|chief legal|\bclo\b", "Bosh yurist"),
          (r"chief marketing|\bcmo\b", "CMO"), (r"chairman|chair\b", "Kengash raisi"),
          (r"president", "Prezident"), (r"\bevp\b|executive vice", "EVP"), (r"\bsvp\b|senior vice", "SVP")]


def role_of(rel):
    roles = []
    title = val(rel, "officerTitle")
    if truthy(val(rel, "isOfficer")) or title:
        short = [lab for pat, lab in TITLES if re.search(pat, title, re.I)]
        roles.append(", ".join(dict.fromkeys(short[:2])) if short else (title[:26] or "Rahbar"))
    if truthy(val(rel, "isDirector")) and not any("Kengash" in r for r in roles):
        roles.append("Kengash a'zosi")
    if truthy(val(rel, "isTenPercentOwner")):
        roles.append("10%+ aksiyador")
    if not roles and truthy(val(rel, "isOther")):
        roles.append(val(rel, "otherText")[:26] or "Boshqa")
    return ", ".join(roles) or "Insayder"


def parse_form4(xml_bytes, accession, filed):
    root = ET.fromstring(xml_bytes)
    owners = [o for o in root if local(o.tag) == "reportingOwner"]
    if not owners:
        return []
    first = owners[0]
    owner_cik = val(first, "reportingOwnerId", "rptOwnerCik") or accession
    name = nice_name(val(first, "reportingOwnerId", "rptOwnerName"))
    if len(owners) > 1:
        name += f" (+{len(owners) - 1})"
    rel = child(first, "reportingOwnerRelationship")
    role = role_of(rel)
    is_10 = truthy(val(rel, "isTenPercentOwner")) and not (truthy(val(rel, "isOfficer"))
                                                           or truthy(val(rel, "isDirector")))
    notes = " ".join((f.text or "") for f in root.iter() if local(f.tag) == "footnote")
    plan = truthy(val(root, "aff10b5One")) or bool(re.search(r"10b5-1", notes, re.I))
    out = []
    table = child(root, "nonDerivativeTable")
    for tx in (table if table is not None else []):
        if local(tx.tag) != "nonDerivativeTransaction":
            continue
        code = val(tx, "transactionCoding", "transactionCode").upper()
        shares = num(val(tx, "transactionAmounts", "transactionShares"))
        price = num(val(tx, "transactionAmounts", "transactionPricePerShare"))
        out.append({
            "accession": accession, "filed": filed, "owner": owner_cik, "name": name, "role": role,
            "ten_pct_only": is_10, "date": val(tx, "transactionDate")[:10] or filed, "code": code,
            "shares": shares, "price": price, "value": shares * price,
            "ad": val(tx, "transactionAmounts", "transactionAcquiredDisposedCode").upper(),
            "after": num(val(tx, "postTransactionAmounts", "sharesOwnedFollowingTransaction")),
            "direct": val(tx, "ownershipNature", "directOrIndirectOwnership").upper() != "I",
            "plan": plan,
        })
    return out


def fetch_transactions(cik, filings):
    def one(f):
        url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{f['accession'].replace('-', '')}/{f['doc']}"
        try:
            return parse_form4(sec_get(url, as_json=False), f["accession"], f["filed"])
        except Exception as exc:
            print(f"skip {f['accession']}: {exc}")
            return []
    with ThreadPoolExecutor(max_workers=6) as pool:
        return [t for chunk in pool.map(one, filings) for t in chunk]


# ----------------------------- Analysis -------------------------------------

def find_clusters(buys):
    """Windows of CLUSTER_WINDOW days with purchases by CLUSTER_MIN+ different insiders (merged)."""
    buys = sorted(buys, key=lambda t: t["date"])
    windows = []
    for i, t in enumerate(buys):
        start = date.fromisoformat(t["date"])
        group = [b for b in buys[i:] if date.fromisoformat(b["date"]) - start <= timedelta(days=CLUSTER_WINDOW)]
        if len({b["owner"] for b in group}) >= CLUSTER_MIN:
            windows.append(group)
    merged = []
    for g in windows:
        if merged and g[0]["date"] <= merged[-1][-1]["date"]:
            seen = {id(x) for x in merged[-1]}
            merged[-1] = sorted(merged[-1] + [x for x in g if id(x) not in seen], key=lambda t: t["date"])
        else:
            merged.append(g)
    out = []
    for g in merged:
        people = {}
        for b in g:
            p = people.setdefault(b["owner"], {"name": b["name"], "role": b["role"], "value": 0.0})
            p["value"] += b["value"]
        out.append({"start": g[0]["date"], "end": g[-1]["date"], "people": list(people.values()),
                    "value": sum(b["value"] for b in g)})
    return out


def analyse(txs, today):
    since = (today - timedelta(days=DAYS)).isoformat()
    recent = (today - timedelta(days=RECENT_DAYS)).isoformat()
    txs = [t for t in txs if t["date"] >= since]
    seen, uniq = set(), []                       # same filing listed twice -> count once
    for t in txs:
        key = (t["accession"], t["date"], t["code"], t["shares"], t["price"], t["after"])
        if key not in seen:
            seen.add(key)
            uniq.append(t)
    buys = [t for t in uniq if t["code"] == "P" and t["shares"] > 0]
    sells = [t for t in uniq if t["code"] == "S" and t["shares"] > 0]
    others = {}
    for t in uniq:
        if t["code"] not in ("P", "S"):
            others[t["code"]] = others.get(t["code"], 0) + 1

    people = {}
    for t in sorted(buys + sells, key=lambda t: t["date"]):
        p = people.setdefault(t["owner"], {"name": t["name"], "role": t["role"], "buy": 0.0, "buy_sh": 0.0,
                                           "sell": 0.0, "sell_sh": 0.0, "n_buy": 0, "n_sell": 0,
                                           "plan_sell": 0.0, "last": t["date"], "after": t["after"]})
        side = "buy" if t["code"] == "P" else "sell"
        p[side] += t["value"]
        p[side + "_sh"] += t["shares"]
        p["n_" + side] += 1
        if side == "sell" and t["plan"]:
            p["plan_sell"] += t["value"]
        p["last"], p["after"] = t["date"], t["after"]
    insiders = sorted(people.values(), key=lambda p: (-(p["buy"] > 0), -(p["buy"] + p["sell"])))

    months = []
    m = date(today.year, today.month, 1)
    for _ in range(6):
        months.insert(0, m)
        m = date(m.year - (m.month == 1), (m.month - 2) % 12 + 1, 1)
    monthly = []
    for m in months:
        key = m.isoformat()[:7]
        monthly.append({"label": MONTHS[m.month - 1],
                        "buy": sum(t["value"] for t in buys if t["date"][:7] == key),
                        "sell": sum(t["value"] for t in sells if t["date"][:7] == key)})

    s = {
        "buy": sum(t["value"] for t in buys), "sell": sum(t["value"] for t in sells),
        "buy_people": len({t["owner"] for t in buys}), "sell_people": len({t["owner"] for t in sells}),
        "buy_3m": sum(t["value"] for t in buys if t["date"] >= recent),
        "sell_3m": sum(t["value"] for t in sells if t["date"] >= recent),
        "plan_sell": sum(t["value"] for t in sells if t["plan"]),
        "n_buy": len(buys), "n_sell": len(sells),
    }
    clusters = find_clusters([t for t in buys if not t["ten_pct_only"]])
    return {"buys": sorted(buys, key=lambda t: t["date"], reverse=True), "sells": sells, "others": others,
            "insiders": insiders, "monthly": monthly, "sum": s, "clusters": clusters, "n_tx": len(uniq)}


def verdict(a):
    s, cl = a["sum"], a["clusters"]
    if cl and max(len(c["people"]) for c in cl) >= 3:
        return "strong", "KUCHLI KLASTER XARID", "3+ rahbar qisqa vaqtda o'z pulini tikdi"
    if cl:
        return "buy", "KLASTER XARID", "bir necha rahbar birga sotib oldi"
    if s["buy"] > 0 and s["buy"] >= s["sell"]:
        return "buy", "XARIDLAR USTUN", "rahbarlar sotganidan ko'proq sotib oldi"
    if s["sell"] > 0 and s["buy"] == 0 and s["plan_sell"] >= 0.7 * s["sell"]:
        return "plan", "REJALI SOTISHLAR", "sotishlarning ko'pi oldindan rejalashtirilgan (10b5-1)"
    if s["sell"] > 0:
        return "sell", "SOTISHLAR USTUN", "ochiq bozorda asosan sotishlar"
    return "none", "SAVDO YO'Q", "ochiq bozorda xarid ham, sotish ham yo'q"


# ----------------------------- Formatting -----------------------------------

def big(v):
    a = abs(v)
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            return f"{v / div:,.1f}{suf}"
    return f"{v:,.0f}"


def esc(s):
    return html.escape(str(s), quote=False)


def dmy(iso):
    return datetime.fromisoformat(iso).strftime("%d.%m.%Y")


def format_text(symbol, name, price, a):
    s = a["sum"]
    kind, title, why = verdict(a)
    icon = {"strong": "🔥", "buy": "🟢", "plan": "🟡", "sell": "🔴", "none": "⚪"}[kind]
    head = f"👔 <b>${esc(symbol)} — insayder savdolari</b>\n<i>{esc(name)}"
    head += f" · narx ${price:,.2f}</i>" if price else "</i>"
    lines = [head, "", f"{icon} <b>Xulosa: {title}</b> — {why}", "",
             f"📊 <b>So'nggi 6 oy (ochiq bozor)</b>",
             f"   🟢 Sotib olish: <b>${big(s['buy'])}</b> · {s['buy_people']} ta insayder · {s['n_buy']} ta bitim",
             f"   🔴 Sotish: <b>${big(s['sell'])}</b> · {s['sell_people']} ta insayder · {s['n_sell']} ta bitim"]
    if s["sell"]:
        lines.append(f"   📅 Sotishning {s['plan_sell'] / s['sell'] * 100:.0f}% i 10b5-1 reja bo'yicha")
    lines.append(f"   ⏱ So'nggi 3 oy: xarid ${big(s['buy_3m'])} · sotish ${big(s['sell_3m'])}")

    if a["clusters"]:
        lines += ["", "🔥 <b>Klaster xaridlar</b>"]
        for c in a["clusters"][-3:]:
            names = ", ".join(f"{esc(p['name'])} ({esc(p['role'])})" for p in c["people"][:5])
            lines.append(f"   {dmy(c['start'])}–{dmy(c['end'])}: <b>{len(c['people'])} ta insayder</b> · "
                         f"${big(c['value'])}\n   {names}")

    if a["buys"]:
        lines += ["", "🟢 <b>Ochiq bozorda sotib olishlar</b>"]
        for t in a["buys"][:10]:
            lines.append(f"   {dmy(t['date'])} · <b>{esc(t['name'])}</b> ({esc(t['role'])})\n"
                         f"      {big(t['shares'])} aksiya × ${t['price']:,.2f} = <b>${big(t['value'])}</b>"
                         f" · keyin {big(t['after'])} aksiya")
        if len(a["buys"]) > 10:
            lines.append(f"   … yana {len(a['buys']) - 10} ta xarid")

    sellers = sorted([p for p in a["insiders"] if p["sell"]], key=lambda p: -p["sell"])
    if sellers:
        lines += ["", "🔴 <b>Eng ko'p sotganlar</b>"]
        for p in sellers[:8]:
            avg = p["sell"] / p["sell_sh"] if p["sell_sh"] else 0
            plan = " · 📅 reja" if p["plan_sell"] >= 0.5 * p["sell"] else ""
            lines.append(f"   <b>{esc(p['name'])}</b> ({esc(p['role'])})\n"
                         f"      ${big(p['sell'])} · {p['n_sell']} ta bitim · o'rtacha ${avg:,.2f}"
                         f" · keyin {big(p['after'])} aksiya{plan}")
        if len(sellers) > 8:
            lines.append(f"   … yana {len(sellers) - 8} ta sotuvchi")

    if a["others"]:
        parts = [f"{OTHER_CODES.get(c, c)}: {n}" for c, n in sorted(a["others"].items(), key=lambda x: -x[1])]
        lines += ["", "⚙️ <b>Boshqa harakatlar</b> (bozor signali emas)", "   " + " · ".join(parts)]

    lines += ["", f"<i>{esc(DISCLAIMER)}</i>"]
    return "\n".join(lines)


# ----------------------------- Chart ----------------------------------------

THEME = {"bg": "#0b1220", "panel": "#111a2e", "line": "#1e2a44", "ink": "#e6edf7", "muted": "#8a99b8",
         "buy": "#22c55e", "sell": "#ef4444", "plan": "#f59e0b", "strong": "#38bdf8", "none": "#94a3b8"}


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


def make_chart(symbol, name, price, a, today):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import FancyBboxPatch

    T = THEME
    plt.rcParams["font.family"] = _font()
    s = a["sum"]
    rows = a["insiders"][:8]
    n = max(len(rows), 1)
    fig_h = 6.4 + 0.5 * n
    fig = plt.figure(figsize=(10, fig_h), dpi=160, facecolor=T["bg"])
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_axis_off()
    ax.set_xlim(0, 1)
    ax.set_ylim(0, fig_h)
    y = fig_h

    ax.text(0.04, y - 0.55, f"${symbol}", fontsize=28, fontweight="bold", color=T["ink"], va="center")
    ax.text(0.04, y - 1.03, name[:44], fontsize=12.5, color=T["ink"], va="center")
    sub = f"Insayder savdolari · SEC Form 4 · so'nggi 6 oy · {today:%d.%m.%Y}"
    ax.text(0.04, y - 1.40, sub, fontsize=9.5, color=T["muted"], va="center")
    for x, label, value, extra, color in (
            (0.56, "Sotib olish", f"${big(s['buy'])}", f"{s['buy_people']} ta insayder", T["buy"]),
            (0.78, "Sotish", f"${big(s['sell'])}", f"{s['sell_people']} ta insayder", T["sell"])):
        ax.text(x, y - 0.42, label, fontsize=9.5, color=T["muted"], va="center")
        ax.text(x, y - 0.84, value, fontsize=21, fontweight="bold", color=color, va="center")
        ax.text(x, y - 1.22, extra, fontsize=9, color=T["muted"], va="center")

    kind, title, why = verdict(a)
    color = T[{"strong": "strong", "buy": "buy", "plan": "plan", "sell": "sell", "none": "none"}[kind]]
    ax.add_patch(FancyBboxPatch((0.04, y - 2.18), 0.92, 0.5, boxstyle="round,pad=0,rounding_size=0.06",
                                facecolor=T["panel"], edgecolor=color, linewidth=1.3))
    ax.text(0.065, y - 1.93, title, fontsize=12, fontweight="bold", color=color, va="center")
    ax.text(0.94, y - 1.93, why, fontsize=9.5, color=T["ink"], va="center", ha="right")

    # monthly bars: purchases up (green), sales down (red), same scale
    bx = fig.add_axes([0.08, (y - 5.05) / fig_h, 0.86, 2.35 / fig_h], facecolor=T["bg"])
    labels = [m["label"] for m in a["monthly"]]
    buys = [m["buy"] for m in a["monthly"]]
    sells = [-m["sell"] for m in a["monthly"]]
    top_b = max(buys + [1])                       # purchases are usually far smaller than sales,
    top_s = max([-v for v in sells] + [1])         # so each side has its own scale (labels show $)
    hb = [v / top_b for v in buys]
    hs = [v / top_s for v in sells]
    xs = range(len(labels))
    bx.bar(xs, hb, color=T["buy"], width=0.55)
    bx.bar(xs, hs, color=T["sell"], width=0.55)
    bx.axhline(0, color=T["line"], linewidth=1)
    for i, (b, sv, h1, h2) in enumerate(zip(buys, sells, hb, hs)):
        if b:
            bx.text(i, h1 + 0.05, f"${big(b)}", ha="center", va="bottom", fontsize=8.5, color=T["buy"])
        if sv:
            bx.text(i, h2 - 0.05, f"${big(-sv)}", ha="center", va="top", fontsize=8.5, color=T["sell"])
    top = 1
    bx.set_ylim(-top * 1.35, top * 1.35)
    bx.set_xticks(list(xs), labels, color=T["muted"], fontsize=9.5)
    bx.set_yticks([])
    bx.tick_params(length=0)
    for sp in bx.spines.values():
        sp.set_visible(False)
    ax.text(0.04, y - 2.55, "Oylar bo'yicha:  ▲ xarid   ▼ sotish   (har biri o'z masshtabida)", fontsize=9, color=T["muted"], va="center")

    # insiders table
    ty = y - 5.45
    ax.plot([0.04, 0.96], [ty + 0.18, ty + 0.18], color=T["line"], linewidth=1)
    ax.text(0.04, ty - 0.05, "Insayder", fontsize=9, color=T["muted"], va="center")
    ax.text(0.955, ty - 0.05, "6 oyda", fontsize=9, color=T["muted"], va="center", ha="right")
    if not rows:
        ax.text(0.5, ty - 0.55, "Ochiq bozorda xarid yoki sotish yo'q", fontsize=11, color=T["muted"],
                ha="center", va="center")
    for k, p in enumerate(rows):
        cy = ty - 0.5 - k * 0.5
        if k % 2 == 0:
            ax.add_patch(plt.Rectangle((0.03, cy - 0.25), 0.94, 0.5, facecolor=T["panel"], edgecolor="none"))
        ax.text(0.04, cy + 0.08, p["name"][:34], fontsize=11, color=T["ink"], va="center")
        ax.text(0.04, cy - 0.13, p["role"][:46], fontsize=8.5, color=T["muted"], va="center")
        pills = []
        if p["buy"]:
            pills.append((f"SOTIB OLDI ${big(p['buy'])}", T["buy"]))
        if p["sell"]:
            tag = " · REJA" if p["plan_sell"] >= 0.5 * p["sell"] else ""
            pills.append((f"SOTDI ${big(p['sell'])}{tag}", T["sell"]))
        x = 0.955
        for text, c in pills:
            t = ax.text(x, cy, text, fontsize=9.5, fontweight="bold", color=c, va="center", ha="right",
                        bbox=dict(boxstyle="round,pad=0.35,rounding_size=0.6", facecolor=T["bg"], edgecolor=c,
                                  linewidth=1.1))
            x -= 0.03 + 0.0115 * len(text)
    ax.text(0.04, 0.3, "Form 4: savdodan keyin 2 ish kunida topshiriladi · REJA = 10b5-1 oldindan rejalashtirilgan",
            fontsize=8, color=T["muted"], va="center")
    ax.text(0.96, 0.3, "Trading Laboratory", fontsize=8, color=T["muted"], va="center", ha="right")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=T["bg"])
    plt.close(fig)
    return buf.getvalue()


# ----------------------------- Main -----------------------------------------

def last_price(symbol):
    try:
        import yfinance as yf
        p = yf.Ticker(symbol).fast_info.get("lastPrice")
        return float(p) if p else 0.0
    except Exception:
        return 0.0


def main():
    symbol = TICKER_IN.upper().lstrip("$").replace(".", "-")
    if not re.fullmatch(r"[A-Z][A-Z\-]{0,7}", symbol):
        send_text(f"❌ \"{TICKER_IN}\" — noto'g'ri tiker. Masalan: $AAPL")
        return
    if "@" not in SEC_CONTACT:
        raise RuntimeError("SEC_CONTACT secret is missing")
    cik, title = find_company(symbol)
    if not cik:
        send_text(f"ℹ️ ${symbol} SEC ro'yxatida topilmadi. Form 4 faqat AQShda ro'yxatdan o'tgan "
                  "kompaniyalar uchun bor (ADR va xorijiy kompaniyalar topshirmaydi).")
        return
    today = datetime.now(timezone.utc).date()
    sec_name, filings = form4_list(cik, (today - timedelta(days=DAYS + 5)).isoformat())
    print(f"{symbol}: CIK {cik} {sec_name}, {len(filings)} Form 4 filings")
    txs = fetch_transactions(cik, filings)
    a = analyse(txs, today)
    name = sec_name or title
    price = last_price(symbol)
    s = a["sum"]
    print(f"{len(txs)} transactions, buys ${s['buy']:,.0f}, sells ${s['sell']:,.0f}, clusters {len(a['clusters'])}")
    kind, vtitle, _ = verdict(a)
    caption = (f"👔 <b>${esc(symbol)} — insayder savdolari</b>\n"
               f"6 oyda: 🟢 ${big(s['buy'])} xarid · 🔴 ${big(s['sell'])} sotish\n<b>{vtitle}</b>")
    if s["n_buy"] or s["n_sell"]:
        send_photo(make_chart(symbol, name, price, a, today), caption)
        send_text(format_text(symbol, name, price, a), reply=False, html_mode=True)
    else:
        send_text(format_text(symbol, name, price, a), html_mode=True)     # nothing to draw


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
