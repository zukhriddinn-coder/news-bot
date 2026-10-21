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
import urllib.request
from calendar import timegm

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

MAX_POSTS_PER_RUN = 12       # at most this many posts every run (avoids spam)
SUMMARY_LENGTH = 280         # max characters of the short description
MAX_AGE_HOURS = 12           # ignore articles older than this
SEEN_FILE = "seen.json"      # remembers which news were already posted
SEEN_LIMIT = 8000            # how many old links to remember

# ---------------------------------------------------------------------------

BOT_TOKEN = re.sub(r"\s+", "", os.environ["BOT_TOKEN"])
CHAT_ID = re.sub(r"\s+", "", os.environ["CHAT_ID"])
THREAD_ID = re.sub(r"\s+", "", os.environ["THREAD_ID"])
if CHAT_ID.isdigit():          # "1987654321" -> "-1001987654321"
    CHAT_ID = "-100" + CHAT_ID

socket.setdefaulttimeout(20)
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
    title = html.escape(item["title"])
    summary = html.escape(item["summary"])
    link = html.escape(item["link"], quote=True)
    parts = [f"<b>{title}</b>"]
    if summary and summary.lower() != item["title"].lower():
        parts.append(summary)
    parts.append(f"{item['tag']}\n🔗 <a href=\"{link}\">Source: {html.escape(item['source'])}</a>")
    return "\n\n".join(parts)


def main():
    seen = load_seen()
    if not seen["connected"] and not diagnose():
        raise SystemExit("Sozlamalarni yuqoridagi TEKSHIRUV natijasiga qarab to'g'rilang.")
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
            if first_time:
                # new feed: remember old articles, don't post them
                seen["links"].append(link)
                seen_links.add(link)
                continue
            ts = entry_time(e)
            if ts and now - ts > MAX_AGE_HOURS * 3600:
                seen["links"].append(link)
                seen_links.add(link)
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

    if not seen["connected"]:
        send_post("✅ <b>News bot connected.</b>\nNew trading, macro and stock market news will appear here.")
        seen["connected"] = True

    # remove duplicates with the same title (same story in two feeds)
    unique, titles = [], set()
    for item in sorted(new_items, key=lambda x: x["time"]):
        key = item["title"].lower()
        if key in titles:
            seen["links"].append(item["link"])
            continue
        titles.add(key)
        unique.append(item)

    posted, errors = 0, 0
    for item in unique[:MAX_POSTS_PER_RUN]:
        try:
            send_post(format_post(item))
            seen["links"].append(item["link"])
            posted += 1
            time.sleep(3)
        except Exception as exc:
            errors += 1
            print(f"ERROR sending '{item['title']}': {exc}")

    save_seen(seen)
    print(f"Posted {posted}, waiting {max(len(unique) - MAX_POSTS_PER_RUN, 0)}, errors {errors}")
    if errors and not posted:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
