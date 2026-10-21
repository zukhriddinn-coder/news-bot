"""
Trading Laboratory -> news for one ticker
A subscriber writes $TICKER in the News topic. Google Apps Script starts this script
(GitHub Actions) with the ticker. It finds the latest news about that company on the
sites CNBC, MarketWatch, Investing.com and Financial Times,
translates the headlines to Uzbek and replies in the topic.
"""

import html
import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from calendar import timegm
from datetime import datetime
from zoneinfo import ZoneInfo

import feedparser
import yfinance as yf

# ---------------------------------------------------------------------------
# SETTINGS (you can edit these)
# ---------------------------------------------------------------------------

MAX_NEWS = 10                 # news per answer
PER_SITE = 4                  # at most this many from one site, unless others have nothing
DAYS = 7                      # how far back to look
SITES = {                     # domain -> name shown in the answer
    "cnbc.com": "CNBC",
    "marketwatch.com": "MarketWatch",
    "investing.com": "Investing.com",
    "ft.com": "Financial Times",
}
LOCAL_TZ = "Asia/Tashkent"    # times are shown in this time zone
GEMINI_MODELS = ["gemini-flash-latest", "gemini-2.5-flash", "gemini-flash-lite-latest",
                 "gemini-2.5-flash-lite"]

# ---------------------------------------------------------------------------

BOT_TOKEN = re.sub(r"\s+", "", os.environ.get("BOT_TOKEN", ""))
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
TICKER_IN = os.environ.get("TICKER", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()
THREAD_ID = os.environ.get("THREAD_ID", "").strip()
REPLY_TO = os.environ.get("REPLY_TO", "").strip()
ACK_ID = os.environ.get("ACK_ID", "").strip()
UA = "Mozilla/5.0 (compatible; TradingLabNewsBot/1.0)"


# ----------------------------- Telegram -------------------------------------

def tg(method, payload):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = json.dumps(payload).encode("utf-8")
    for _ in range(3):
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
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


def reply(text, html_mode=False):
    p = {"chat_id": CHAT_ID, "text": text, "link_preview_options": {"is_disabled": True}}
    if THREAD_ID:
        p["message_thread_id"] = int(THREAD_ID)
    if REPLY_TO:
        p["reply_parameters"] = {"message_id": int(REPLY_TO), "allow_sending_without_reply": True}
    if html_mode:
        p["parse_mode"] = "HTML"
    tg("sendMessage", p)


def delete_ack():
    if ACK_ID:
        try:
            tg("deleteMessage", {"chat_id": CHAT_ID, "message_id": int(ACK_ID)})
        except Exception as exc:
            print(f"Could not delete the waiting message: {exc}")


# ----------------------------- Company --------------------------------------

SUFFIXES = re.compile(
    r"[,\s]+(inc\.?|incorporated|corp\.?|corporation|company|co\.?|ltd\.?|limited|plc|holdings?|"
    r"group|n\.v\.|s\.a\.|ag|se|class [a-z]|the)$", re.I)


def short_name(long_name):
    name = re.sub(r"^the\s+", "", long_name.strip(), flags=re.I)
    for _ in range(4):
        new = SUFFIXES.sub("", name).strip(" ,.&")
        if new == name:
            break
        name = new
    return name or long_name


def company(symbol):
    info = yf.Ticker(symbol).info or {}
    name = info.get("longName") or info.get("shortName")
    return name if name else None


# ----------------------------- News search ----------------------------------

def google_news(symbol, name):
    sites = " OR ".join(f"site:{d}" for d in SITES)
    query = f'("{name}" OR "{symbol}") when:{DAYS}d ({sites})'
    url = ("https://news.google.com/rss/search?hl=en-US&gl=US&ceid=US:en&q="
           + urllib.parse.quote(query))
    feed = feedparser.parse(url, agent=UA)
    items = []
    for e in feed.entries:
        src = e.get("source") or {}
        domain = urllib.parse.urlparse(src.get("href", "")).netloc.lower()
        site = next((v for k, v in SITES.items() if domain.endswith(k)), None)
        if not site:
            continue
        title = e.get("title", "")
        suffix = " - " + (src.get("title") or "")
        if title.endswith(suffix):
            title = title[: -len(suffix)]
        t = e.get("published_parsed")
        items.append({"title": clean_title(title), "link": e.get("link"), "site": site,
                      "time": timegm(t) if t else None})
    print(f"Google News: {len(feed.entries)} results, {len(items)} from our sites")
    return items


def yahoo_news(symbol):
    """Yahoo's own list of news for the ticker, kept only when it comes from our sites."""
    items = []
    try:
        news = yf.Ticker(symbol).news or []
    except Exception as exc:
        print(f"Yahoo news: {exc}")
        return items
    for n in news:
        c = n.get("content", n)
        provider = (c.get("provider") or {}).get("displayName") or c.get("publisher") or ""
        link = ((c.get("canonicalUrl") or {}).get("url") or c.get("link") or "")
        domain = urllib.parse.urlparse(link).netloc.lower()
        site = next((v for k, v in SITES.items() if domain.endswith(k) or provider.lower() == v.lower()), None)
        if not site:
            continue
        ts = c.get("providerPublishTime")
        if not ts and c.get("pubDate"):
            try:
                ts = int(datetime.fromisoformat(c["pubDate"].replace("Z", "+00:00")).timestamp())
            except ValueError:
                ts = None
        items.append({"title": clean_title(c.get("title") or ""), "link": link, "site": site, "time": ts})
    print(f"Yahoo news: {len(news)} results, {len(items)} from our sites")
    return items


SITE_WORDS = r"(investing\.com|cnbc|marketwatch|financial times|ft\.com)"


def clean_title(title):
    """Removes the site name that some sites add to their headlines."""
    t = re.sub(r"\s*[-—–|:]?\s*(by\s+)?" + SITE_WORDS + r"(['’]s)?(\s+(take|analysis|talqinida|tahlili|tahliliga ko['‘’]ra|ma['‘’]lumotlariga ko['‘’]ra))?\s*$",
               "", title.strip(), flags=re.I)
    t = re.sub(r"^" + SITE_WORDS + r"\s*[-—–|:]\s*", "", t, flags=re.I)
    return re.sub(r"\s+", " ", t).strip()


STOP = {"a", "an", "the", "is", "are", "was", "were", "be", "to", "of", "in", "on", "for", "and", "or",
        "its", "it", "this", "that", "with", "as", "at", "by", "from", "s", "according", "take", "says"}
SYNONYMS = {"shares": "stock", "stocks": "stock", "share": "stock", "equity": "stock",
            "rises": "rise", "rising": "rise", "soars": "soar", "soaring": "soar", "jumps": "jump",
            "falls": "fall", "falling": "fall", "drops": "drop", "dropping": "drop"}


def norm(title):
    """The headline as a set of its meaningful words (site names and filler words removed)."""
    t = unicodedata.normalize("NFKD", title.lower())
    t = re.sub(SITE_WORDS, " ", t)
    words = re.sub(r"[^a-z0-9]+", " ", t).split()
    return frozenset(SYNONYMS.get(w, w) for w in words if w not in STOP)


def same_story(a, b):
    """Two headlines are one story when 80% of their meaningful words are the same."""
    if not a or not b:
        return a == b
    return len(a & b) / len(a | b) >= 0.8


def relevant(item, symbol, name):
    text = f"{item['title']} {item.get('summary', '')}"
    return (re.search(rf"\b{re.escape(name)}\b", text, re.I) is not None
            or re.search(rf"(\$|\b){re.escape(symbol)}\b", text) is not None)


def find_news(symbol, name):
    now = time.time()
    found = []
    for source in (lambda: google_news(symbol, name), lambda: yahoo_news(symbol)):
        try:
            found += source()
        except Exception as exc:
            print(f"Source error: {exc}")

    # newest first, without old, unrelated or repeated stories
    unique = []
    for it in sorted(found, key=lambda x: x.get("time") or 0, reverse=True):
        if not it.get("title") or not it.get("link") or not relevant(it, symbol, name):
            continue
        if it.get("time") and now - it["time"] > DAYS * 86400:
            continue
        key = norm(it["title"])
        if any(same_story(key, u["key"]) for u in unique):
            continue
        unique.append({**it, "key": key})

    # mix the sites: at most PER_SITE from one site first, then fill up with the rest
    picked, per_site = [], {}
    for it in unique:
        if per_site.get(it["site"], 0) < PER_SITE:
            picked.append(it)
            per_site[it["site"]] = per_site.get(it["site"], 0) + 1
    for it in unique:
        if len(picked) >= MAX_NEWS:
            break
        if it not in picked:
            picked.append(it)
    picked = sorted(picked[:MAX_NEWS], key=lambda x: x.get("time") or 0, reverse=True)
    print(f"Found {len(found)}, unique {len(unique)}, sent {len(picked)}: {per_site}")
    return picked


# ----------------------------- Translation ----------------------------------

def gemini_titles(titles):
    if not GEMINI_API_KEY or not titles:
        return {}
    prompt = ("You are a professional financial news translator. Translate each headline into Uzbek "
              "(Latin script), natural and clear. Keep tickers, company and people names, numbers and "
              "currencies unchanged. Leave out website names such as Investing.com or CNBC. Return ONLY a JSON array: [{\"id\": <id>, \"title\": \"...\"}].\n\n"
              + json.dumps([{"id": i, "title": t} for i, t in enumerate(titles)], ensure_ascii=False))
    payload = json.dumps({"contents": [{"parts": [{"text": prompt}]}],
                          "generationConfig": {"temperature": 0.2,
                                               "responseMimeType": "application/json"}}).encode("utf-8")
    for model in GEMINI_MODELS:
        for attempt in range(2):
            req = urllib.request.Request(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                data=payload, headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY})
            try:
                with urllib.request.urlopen(req, timeout=90) as resp:
                    res = json.load(resp)
                out = json.loads(res["candidates"][0]["content"]["parts"][0]["text"])
                return {int(o["id"]): str(o["title"]).strip() for o in out if str(o.get("title", "")).strip()}
            except urllib.error.HTTPError as e:
                msg = e.read().decode("utf-8", "replace")[:200]
                print(f"Gemini {model}: HTTP {e.code} {msg}")
                if e.code in (401, 403) or "API key" in msg:
                    return {}
                if e.code == 404 or (e.code == 429 and "PerDay" in msg):
                    break
                time.sleep(15 * (attempt + 1))
            except Exception as exc:
                print(f"Gemini {model}: {exc}")
                time.sleep(5)
    return {}


def google_translate(text):
    url = ("https://translate.googleapis.com/translate_a/single?client=gtx&sl=en&tl=uz&dt=t&q="
           + urllib.parse.quote(text))
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    return "".join(part[0] for part in data[0] if part and part[0]).strip()


def translate(items):
    todo = [i for i, it in enumerate(items) if not it.get("uz")]
    result = gemini_titles([items[i]["title"] for i in todo])
    for k, i in enumerate(todo):
        if k in result:
            items[i]["uz"] = result[k]
            continue
        try:
            items[i]["uz"] = google_translate(items[i]["title"])
        except Exception:
            items[i]["uz"] = items[i]["title"]          # keep English if nothing works


# ----------------------------- Message --------------------------------------

def no_autolink(text):
    """Telegram turns words like Investing.com into links; a word joiner after the dot stops it."""
    return re.sub(r"\b([A-Za-z0-9-]+)\.(com|net|org|io|co)\b", "\\1.\u2060\\2", text)


def format_answer(symbol, name, items):
    tz = ZoneInfo(LOCAL_TZ)
    lines = [f"📰 <b>${html.escape(symbol)} — {html.escape(name)}</b>",
             f"<i>Oxirgi {DAYS} kundagi yangiliklar · {no_autolink(', '.join(SITES.values()))}</i>", ""]
    for i, it in enumerate(items, 1):
        when = datetime.fromtimestamp(it["time"], tz).strftime("%d.%m, %H:%M") if it.get("time") else ""
        meta = " · ".join(x for x in (it["site"], when) if x)
        link = html.escape(it["link"], quote=True)
        title = clean_title(it.get("uz") or it["title"])
        lines.append(f"{i}. <b>{no_autolink(html.escape(title))}</b>")
        lines.append(f"    <i>{no_autolink(html.escape(meta))}</i> · <a href=\"{link}\">Manba</a>")
        lines.append("")
    return "\n".join(lines).strip()


# ----------------------------- Main -----------------------------------------

def main():
    symbol = TICKER_IN.upper().lstrip("$").replace(".", "-")
    if not re.fullmatch(r"[A-Z][A-Z\-]{0,7}", symbol):
        reply(f"❌ \"{TICKER_IN}\" — noto'g'ri tiker. Masalan: $TSLA")
        return
    long_name = company(symbol)
    if not long_name:
        reply(f"❌ ${symbol} topilmadi. Tikerni tekshiring (masalan: $AAPL, $TSLA).")
        return
    name = short_name(long_name)
    print(f"{symbol}: {long_name} -> searching for '{name}'")

    items = find_news(symbol, name)
    if not items:
        reply(f"ℹ️ Oxirgi {DAYS} kunda ${symbol} ({long_name}) bo'yicha "
              f"{', '.join(SITES.values())} saytlarida yangilik topilmadi.")
        return
    translate(items)
    reply(format_answer(symbol, long_name, items), html_mode=True)
    print(f"Sent {len(items)} news")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}")
        try:
            reply(f"❌ ${TICKER_IN.upper().lstrip('$')} bo'yicha yangiliklarni olib bo'lmadi. "
                  "Birozdan keyin qayta urinib ko'ring.")
        finally:
            delete_ack()
            raise SystemExit(1)
    delete_ack()
