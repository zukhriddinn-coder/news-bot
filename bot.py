"""
Trading Laboratory -> News bot
Checks financial RSS feeds and posts new articles to a Telegram forum topic.
Runs on GitHub Actions every ~10 minutes (see .github/workflows/news.yml).
"""

import html
import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from calendar import timegm
from datetime import datetime
from zoneinfo import ZoneInfo

import feedparser

# ---------------------------------------------------------------------------
# SETTINGS (you can edit these)
# ---------------------------------------------------------------------------

FEEDS = [
    # CNBC
    {"name": "CNBC"            , "url": "https://www.cnbc.com/id/100003114/device/rss/rss.html", "tag": "#Markets"},
    {"name": "CNBC"            , "url": "https://www.cnbc.com/id/20910258/device/rss/rss.html", "tag": "#Macro"},
    {"name": "CNBC"            , "url": "https://www.cnbc.com/id/10000664/device/rss/rss.html", "tag": "#Markets"},
    {"name": "CNBC"            , "url": "https://www.cnbc.com/id/15839069/device/rss/rss.html", "tag": "#Trading"},
    {"name": "CNBC"            , "url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20409666", "tag": "#MarketInsider"},
    {"name": "CNBC"            , "url": "https://www.cnbc.com/id/15839135/device/rss/rss.html", "tag": "#Earnings"},
    {"name": "CNBC"            , "url": "https://www.cnbc.com/id/19836768/device/rss/rss.html", "tag": "#Energy"},
    {"name": "CNBC"            , "url": "https://www.cnbc.com/id/10000030/device/rss/rss.html", "tag": "#Oil"},
    {"name": "CNBC"            , "url": "https://www.cnbc.com/id/19854910/device/rss/rss.html", "tag": "#Tech"},
    {"name": "CNBC"            , "url": "https://www.cnbc.com/id/28282083/device/rss/rss.html", "tag": "#Options"},
    {"name": "CNBC"            , "url": "https://www.cnbc.com/id/19832390/device/rss/rss.html", "tag": "#Asia"},
    {"name": "CNBC"            , "url": "https://www.cnbc.com/id/19794221/device/rss/rss.html", "tag": "#Europe"},
    # MarketWatch
    {"name": "MarketWatch"     , "url": "https://feeds.marketwatch.com/marketwatch/topstories", "tag": "#StockMarket"},
    {"name": "MarketWatch"     , "url": "https://feeds.marketwatch.com/marketwatch/bulletins", "tag": "#Breaking"},
    # Investing.com
    {"name": "Investing.com"   , "url": "https://www.investing.com/rss/news_25.rss", "tag": "#StockMarket"},
    {"name": "Investing.com"   , "url": "https://www.investing.com/rss/news_14.rss", "tag": "#Macro"},
    {"name": "Investing.com"   , "url": "https://www.investing.com/rss/news_95.rss", "tag": "#EconomicData"},
    {"name": "Investing.com"   , "url": "https://www.investing.com/rss/news_1061.rss", "tag": "#AnalystRatings"},
    {"name": "Investing.com"   , "url": "https://www.investing.com/rss/news_1062.rss", "tag": "#Earnings"},
    {"name": "Investing.com"   , "url": "https://www.investing.com/rss/news_1.rss", "tag": "#Forex"},
    {"name": "Investing.com"   , "url": "https://www.investing.com/rss/news_11.rss", "tag": "#Commodities"},
    {"name": "Investing.com"   , "url": "https://www.investing.com/rss/news_357.rss", "tag": "#InsiderTrading"},
    {"name": "Investing.com"   , "url": "https://www.investing.com/rss/news_356.rss", "tag": "#Companies"},
    {"name": "Investing.com"   , "url": "https://www.investing.com/rss/news_1065.rss", "tag": "#InvestmentIdeas"},
    {"name": "Investing.com"   , "url": "https://www.investing.com/rss/news_301.rss", "tag": "#Crypto"},
    # Financial Times
    {"name": "Financial Times" , "url": "https://www.ft.com/markets?format=rss", "tag": "#Markets"},
    {"name": "Financial Times" , "url": "https://www.ft.com/global-economy?format=rss", "tag": "#Macro"},
    {"name": "Financial Times" , "url": "https://www.ft.com/commodities?format=rss", "tag": "#Commodities"},
    {"name": "Financial Times" , "url": "https://www.ft.com/emerging-markets?format=rss", "tag": "#EmergingMarkets"},
    {"name": "Financial Times" , "url": "https://www.ft.com/unhedged?format=rss", "tag": "#Unhedged"},
    {"name": "Financial Times" , "url": "https://www.ft.com/companies?format=rss", "tag": "#Companies"},
    # Central banks
    {"name": "Federal Reserve" , "url": "https://www.federalreserve.gov/feeds/press_all.xml", "tag": "#Fed #Macro"},
    {"name": "Federal Reserve" , "url": "https://www.federalreserve.gov/feeds/press_monetary.xml", "tag": "#Fed #FOMC"},
    {"name": "Federal Reserve" , "url": "https://www.federalreserve.gov/feeds/speeches_and_testimony.xml", "tag": "#Fed #Speech"},
    {"name": "Federal Reserve" , "url": "https://www.federalreserve.gov/feeds/s_t_powell.xml", "tag": "#Fed #Powell"},
    {"name": "ECB"             , "url": "https://www.ecb.europa.eu/rss/press.html", "tag": "#ECB #Macro"},
]

SUMMARY_LENGTH = 280         # max characters of the short description
MAX_AGE_HOURS = 12           # ignore articles older than this
SEEN_FILE = "seen.json"      # remembers which news were already posted
SEEN_LIMIT = 8000            # how many old links to remember

# Daily delivery window (New York time). News are collected all day and posted
# only inside this window. Anything not posted by WINDOW_END waits for tomorrow.
MARKET_TZ = "America/New_York"
WINDOW_START = (15, 30)           # 15:30 New York = 30 min before US market close
WINDOW_END = (16, 0)              # 16:00 New York = market close, posting stops
SEND_WEEKDAYS = {0, 1, 2, 3, 4}   # Monday..Friday
QUEUE_MAX_AGE_HOURS = 96          # drop collected news older than this
RECHECK_SECONDS = 120             # inside the window: re-check feeds this often

# Translation to Uzbek (Gemini first, Google Translate as backup, English if both fail)
TRANSLATE = True
TRANSLATE_BATCH = 20              # news translated per Gemini request
GEMINI_MODELS = ["gemini-flash-latest", "gemini-2.5-flash", "gemini-flash-lite-latest",
                 "gemini-2.5-flash-lite", "gemini-2.0-flash"]
GEMINI_RETRIES = 3                # attempts per model before trying the next model
GEMINI_WAIT = 20                  # seconds to wait after a limit/error (grows each retry)
GEMINI_PAUSE = 7                  # pause between batches (free tier allows ~10 per minute)
GEMINI_BUDGET_DAY = 600           # max seconds per run spent on Gemini during the day
GEMINI_BUDGET_WINDOW = 240        # max seconds right before/inside the posting window

# ---------------------------------------------------------------------------

BOT_TOKEN = re.sub(r"\s+", "", os.environ["BOT_TOKEN"])
CHAT_ID = re.sub(r"\s+", "", os.environ["CHAT_ID"])
THREAD_ID = re.sub(r"\s+", "", os.environ["THREAD_ID"])
if CHAT_ID.isdigit():          # "1987654321" -> "-1001987654321"
    CHAT_ID = "-100" + CHAT_ID

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

socket.setdefaulttimeout(60)
USER_AGENT = "Mozilla/5.0 (compatible; TradingLabNewsBot/1.0)"


def load_seen():
    if not os.path.exists(SEEN_FILE):
        return {"links": [], "feeds": [], "connected": False}
    with open(SEEN_FILE, encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("links", [])
    data.setdefault("feeds", [])
    data.setdefault("connected", True)
    return data


def save_seen(data):
    data["links"] = data["links"][-SEEN_LIMIT:]
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def clean_text(raw):
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > SUMMARY_LENGTH:
        text = text[:SUMMARY_LENGTH].rsplit(" ", 1)[0].rstrip(".,;:") + "…"
    return text


def entry_time(entry):
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    return timegm(t) if t else None


def telegram(method, payload):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    body = json.dumps(payload).encode("utf-8")
    for attempt in range(3):
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            info = json.loads(e.read().decode("utf-8") or "{}")
            wait = info.get("parameters", {}).get("retry_after")
            if e.code == 429 and wait:
                time.sleep(wait + 1)
                continue
            raise RuntimeError(f"Telegram error {e.code}: {info.get('description')}")
    raise RuntimeError("Telegram: too many retries")


def send_post(text):
    payload = {
        "chat_id": CHAT_ID,
        "message_thread_id": int(THREAD_ID),
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    }
    telegram("sendMessage", payload)


def diagnose():
    """Checks the bot settings and prints the correct IDs to the log."""
    print("=" * 60)
    print("TEKSHIRUV (diagnostika)")
    me = telegram("getMe", {})["result"]
    print(f"Token ishlayapti. Bot: @{me['username']}")
    print(f"Siz kiritgan CHAT_ID: {CHAT_ID!r}, THREAD_ID: {THREAD_ID!r}")
    try:
        chat = telegram("getChat", {"chat_id": CHAT_ID})["result"]
        print(f"Guruh topildi: {chat.get('title')}")
        return True
    except Exception as exc:
        print(f"Guruh topilmadi: {exc}")

    print("\nBot ko'rgan guruhlar va bo'limlar:")
    found = False
    for upd in telegram("getUpdates", {"allowed_updates": []})["result"]:
        msg = upd.get("message") or upd.get("my_chat_member") or upd.get("channel_post") or {}
        chat = msg.get("chat")
        if not chat or chat.get("type") == "private":
            continue
        found = True
        print(f"  Guruh: {chat.get('title')}  ->  CHAT_ID = {chat['id']}"
              + (f"   THREAD_ID = {msg['message_thread_id']}" if msg.get("message_thread_id") else ""))
    if not found:
        print(f"  Hech narsa topilmadi. @{me['username']} botini guruhga ADMIN qilib qo'shing,")
        print("  News bo'limiga 'test' deb yozing va workflow'ni qayta ishga tushiring.")
    print("=" * 60)
    return False


def format_post(item):
    uz = item.get("uz") or {}
    raw_title = uz.get("title") or item["title"]
    raw_summary = uz.get("summary") if uz else item["summary"]
    title = html.escape(raw_title)
    summary = html.escape(raw_summary or "")
    link = html.escape(item["link"], quote=True)
    parts = [f"<b>{title}</b>"]
    if summary and summary.lower() != raw_title.lower():
        parts.append(summary)
    label = "Manba" if uz else "Source"
    parts.append(f"{item['tag']}\n🔗 <a href=\"{link}\">{label}: {html.escape(item['source'])}</a>")
    return "\n\n".join(parts)


# ----------------------------- Translation ----------------------------------

GEMINI_PROMPT = """You are a professional financial news translator.
Translate each news item below from English into Uzbek (Latin script, modern standard Uzbek).
Rules:
- Natural, clear Uzbek as used by financial journalists; translate meaning, not word by word.
- Keep tickers, company names, people's names, index names (S&P 500, Nasdaq, Dow), currencies and all numbers exactly as in the original.
- Use established Uzbek terms: interest rate = foiz stavkasi, inflation = inflyatsiya, stock = aksiya, earnings = moliyaviy hisobot, bond = obligatsiya, yield = daromadlilik.
- Do not add or remove information.
Return ONLY a JSON array with one object per item: {"id": <same id>, "title": "...", "summary": "..."}.
If an item has an empty summary, return an empty summary.

Items:
"""

_gemini_disabled = False
_gemini_deadline = 0.0            # stop retrying Gemini after this moment (set per run)
_dead_models = set()              # models unavailable or out of daily quota in this run


def _gemini_out_of_time():
    return _gemini_deadline and time.time() > _gemini_deadline


def http_json(url, payload, headers):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.load(resp)


def _gemini_call(model, items):
    data = [{"id": i, "title": it["title"], "summary": it["summary"]} for i, it in enumerate(items)]
    payload = {
        "contents": [{"parts": [{"text": GEMINI_PROMPT + json.dumps(data, ensure_ascii=False)}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    res = http_json(url, payload, {"x-goog-api-key": GEMINI_API_KEY})
    text = res["candidates"][0]["content"]["parts"][0]["text"]
    out = json.loads(text)
    return {int(o["id"]): {"title": str(o.get("title", "")).strip(),
                           "summary": str(o.get("summary", "")).strip()}
            for o in out if str(o.get("title", "")).strip()}


def gemini_translate(items, depth=0):
    """Translates items with Gemini, trying hard: retries with pauses, other models,
    and smaller batches. Returns {index: {"title", "summary"}} for what succeeded."""
    global _gemini_disabled
    if not GEMINI_API_KEY or _gemini_disabled or not items or _gemini_out_of_time():
        return {}
    for model in [m for m in GEMINI_MODELS if m not in _dead_models]:
        for attempt in range(GEMINI_RETRIES):
            if _gemini_out_of_time():
                return {}
            try:
                result = _gemini_call(model, items)
                if len(result) == len(items) or len(items) == 1:
                    return result
                # some items missing: translate the missing ones again
                missing = [i for i in range(len(items)) if i not in result]
                extra = gemini_translate([items[i] for i in missing], depth + 1) if depth < 2 else {}
                for k, v in extra.items():
                    result[missing[k]] = v
                return result
            except urllib.error.HTTPError as e:
                msg = e.read().decode("utf-8", "replace")[:300]
                print(f"Gemini {model}: HTTP {e.code} {msg}")
                if e.code in (401, 403) or "API key" in msg or "API_KEY" in msg:
                    print("Gemini kaliti noto'g'ri yoki ruxsat yo'q. GEMINI_API_KEY secret'ini tekshiring.")
                    _gemini_disabled = True
                    return {}
                if e.code == 404:
                    _dead_models.add(model)
                    break                              # this model is not available, try the next one
                if e.code == 400 and len(items) > 1:
                    break
                if e.code == 429 and "PerDay" in msg:
                    _dead_models.add(model)
                    break                              # daily limit of this model: next model
                time.sleep(GEMINI_WAIT * (attempt + 1))   # 429 limit / 5xx: wait and retry
            except Exception as exc:
                print(f"Gemini {model}: {exc}")
                time.sleep(GEMINI_WAIT * (attempt + 1))
    # still failing: try smaller batches before giving up
    result = {}
    if len(items) > 1 and depth < 2:
        half = len(items) // 2
        left = gemini_translate(items[:half], depth + 1)
        right = gemini_translate(items[half:], depth + 1)
        result = {**left, **{k + half: v for k, v in right.items()}}
    if depth == 0 and not result:
        print("Gemini hozir javob bermayapti, bu ishga tushirishda boshqa urinilmaydi.")
        _gemini_disabled = True
    return result


def google_translate(text):
    if not text:
        return ""
    url = ("https://translate.googleapis.com/translate_a/single?client=gtx&sl=en&tl=uz&dt=t&q="
           + urllib.parse.quote(text))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    return "".join(part[0] for part in data[0] if part and part[0]).strip()


def translate_pending(seen, force=False):
    """Translates queued news that are not translated yet.
    During the day: Gemini only, in full batches (untranslated ones wait and are retried).
    force=True (right before / during posting): translate everything; anything Gemini
    still cannot do goes to Google Translate, and only if that fails too it stays English."""
    if not TRANSLATE:
        return
    queue = seen.get("queue", [])
    pending = [q for q in queue if "uz" not in q]
    if not pending or (len(pending) < TRANSLATE_BATCH and not force):
        return
    global _gemini_deadline
    _gemini_deadline = time.time() + (GEMINI_BUDGET_WINDOW if force else GEMINI_BUDGET_DAY)
    done_gemini = done_google = failed = 0
    for i in range(0, len(pending), TRANSLATE_BATCH):
        batch = pending[i:i + TRANSLATE_BATCH]
        if not force and len(batch) < TRANSLATE_BATCH:
            break
        result = gemini_translate(batch)
        for n, item in enumerate(batch):
            if n in result:
                item["uz"] = result[n]
                done_gemini += 1
            elif force:
                try:                              # backup: Google Translate
                    item["uz"] = {"title": google_translate(item["title"]),
                                  "summary": google_translate(item["summary"])}
                    done_google += 1
                    time.sleep(0.5)
                except Exception as exc:
                    failed += 1
                    item["uz"] = None             # give up: post in English
                    print(f"Translate failed for '{item['title'][:50]}': {exc}")
        time.sleep(GEMINI_PAUSE)                  # stay under the free per-minute limit
    print(f"Translated: Gemini {done_gemini}, Google Translate {done_google}, English {failed}")


def collect(seen):
    """Reads all feeds and adds new articles to the queue. Returns how many were added."""
    seen_links = set(seen["links"])
    now = time.time()
    new_items = []
    for feed in FEEDS:
        parsed = feedparser.parse(feed["url"], agent=USER_AGENT)
        if parsed.get("bozo") and not parsed.entries:
            print(f"WARNING: could not read {feed['name']} ({feed['url']})")
            continue
        first_time = feed["url"] not in seen["feeds"]
        for e in parsed.entries:
            link = e.get("link")
            if not link or link in seen_links:
                continue
            seen_links.add(link)
            ts = entry_time(e)
            if first_time or (ts and now - ts > MAX_AGE_HOURS * 3600):
                seen["links"].append(link)   # old article: remember, don't post
                continue
            new_items.append({
                "title": clean_text(e.get("title")),
                "summary": clean_text(e.get("summary") or e.get("description")),
                "link": link,
                "source": feed["name"],
                "tag": feed["tag"],
                "time": ts or now,
            })
        if first_time:
            seen["feeds"].append(feed["url"])
            print(f"{feed['name']}: first run, saved {len(parsed.entries)} existing articles")

    # add to the queue, skipping the same story coming from two feeds
    queue = seen.setdefault("queue", [])
    titles = {q["title"].lower() for q in queue}
    added = 0
    for item in sorted(new_items, key=lambda x: x["time"]):
        seen["links"].append(item["link"])
        key = item["title"].lower()
        if key in titles:
            continue
        titles.add(key)
        queue.append(item)
        added += 1
    queue[:] = [q for q in queue if now - q["time"] < QUEUE_MAX_AGE_HOURS * 3600]
    translate_pending(seen)
    return added


def window():
    """Returns (now, start, end) in New York time."""
    now = datetime.now(ZoneInfo(MARKET_TZ))
    start = now.replace(hour=WINDOW_START[0], minute=WINDOW_START[1], second=0, microsecond=0)
    end = now.replace(hour=WINDOW_END[0], minute=WINDOW_END[1], second=0, microsecond=0)
    return now, start, end


def in_window():
    now, start, end = window()
    return now.weekday() in SEND_WEEKDAYS and start <= now < end


def main():
    seen = load_seen()
    if not seen["connected"] and not diagnose():
        raise SystemExit("Sozlamalarni yuqoridagi TEKSHIRUV natijasiga qarab to'g'rilang.")
    if not seen["connected"]:
        send_post("✅ <b>News bot connected.</b>\nNew trading, macro and stock market news will appear here.")
        seen["connected"] = True

    added = collect(seen)
    queue = seen["queue"]
    now, start, end = window()
    print(f"New York time {now:%a %H:%M}. Added {added} news, queue {len(queue)}. "
          f"Posting window open: {in_window()}")
    if not in_window() and now.weekday() in SEND_WEEKDAYS and 0 < (start - now).total_seconds() <= 11 * 60:
        translate_pending(seen, force=True)
        save_seen(seen)
        now, start, end = window()
        print(f"Waiting {max(0, (start - now).total_seconds()):.0f}s for the posting window to open…")
        time.sleep(max(0, (start - now).total_seconds()))
    if not in_window():
        save_seen(seen)
        return

    # Inside the window: post the queue, re-check feeds, repeat until the window closes
    posted = errors = 0
    while in_window():
        translate_pending(seen, force=True)
        queue.sort(key=lambda x: x["time"])
        while queue and in_window():
            item = queue.pop(0)
            try:
                send_post(format_post(item))
                posted += 1
            except Exception as exc:
                errors += 1
                print(f"ERROR sending '{item['title']}': {exc}")
            if (posted + errors) % 20 == 0:
                save_seen(seen)
            time.sleep(3)
        if not in_window():
            break
        save_seen(seen)
        now, start, end = window()
        time.sleep(max(1, min(RECHECK_SECONDS, (end - now).total_seconds())))
        if in_window():
            collect(seen)

    save_seen(seen)
    print(f"Window closed. Posted {posted}, errors {errors}, left for tomorrow {len(queue)}")
    if errors and not posted:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
