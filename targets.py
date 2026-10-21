"""
Trading Laboratory -> Analyst price target bot
Watches S&P 500 and NASDAQ-listed stocks on Yahoo Finance and posts to Telegram
when an analyst firm changes its price target or rating.
Runs on GitHub Actions (see .github/workflows/targets.yml).
"""

import csv
import html
import io
import json
import os
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

import yfinance as yf

# ---------------------------------------------------------------------------
# SETTINGS (you can edit these)
# ---------------------------------------------------------------------------

PRIORITY_BATCH = 150     # S&P 500 stocks checked per run (all 500 every ~4 runs)
OTHER_BATCH = 150        # other NASDAQ stocks checked per run
WORKERS = 4              # parallel requests to Yahoo
MAX_AGE_DAYS = 3         # ignore analyst actions older than this
ONLY_TARGET_CHANGES = False  # True = post only when the target price changes

SP500_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
NASDAQ_URL = "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt"
STATE_FILE = "targets_state.json"
LIST_REFRESH_HOURS = 24

# Daily delivery window (New York time). Changes are collected all day and posted
# only inside this window. Anything not posted by WINDOW_END waits for tomorrow.
MARKET_TZ = "America/New_York"
WINDOW_START = (15, 30)           # 15:30 New York = 30 min before US market close
WINDOW_END = (16, 0)              # 16:00 New York = market close, posting stops
SEND_WEEKDAYS = {0, 1, 2, 3, 4}   # Monday..Friday
QUEUE_MAX_AGE_HOURS = 96          # drop collected changes older than this
RECHECK_SECONDS = 120             # inside the window: scan the next batch this often

# ---------------------------------------------------------------------------

BOT_TOKEN = re.sub(r"\s+", "", os.environ.get("TARGETS_BOT_TOKEN", ""))
CHAT_ID = re.sub(r"\s+", "", os.environ.get("TARGETS_CHAT_ID", ""))
THREAD_ID = re.sub(r"\s+", "", os.environ.get("TARGETS_THREAD_ID", ""))   # optional
if not BOT_TOKEN or not CHAT_ID:
    raise SystemExit("TARGETS_BOT_TOKEN va TARGETS_CHAT_ID secret'larini qo'shing.")
if CHAT_ID.isdigit():
    CHAT_ID = "-100" + CHAT_ID

UA = "Mozilla/5.0 (compatible; TradingLabBot/1.0)"
ACTIONS = {
    "up": "upgrade ⬆️",
    "down": "downgrade ⬇️",
    "init": "initiates coverage 🆕",
    "main": "maintains",
    "reit": "reiterates",
}


# ----------------------------- Telegram -------------------------------------

def telegram(method, payload):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    body = json.dumps(payload).encode("utf-8")
    for _ in range(3):
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
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
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    }
    if THREAD_ID:
        payload["message_thread_id"] = int(THREAD_ID)
    telegram("sendMessage", payload)


def diagnose():
    """Prints the bot name and the groups/topics the bot can see."""
    print("=" * 60)
    print("TEKSHIRUV (diagnostika)")
    me = telegram("getMe", {})["result"]
    print(f"Token ishlayapti. Bot: @{me['username']}")
    found = False
    for upd in telegram("getUpdates", {})["result"]:
        msg = upd.get("message") or upd.get("my_chat_member") or upd.get("channel_post") or {}
        chat = msg.get("chat")
        if not chat or chat.get("type") == "private":
            continue
        found = True
        print(f"  Guruh: {chat.get('title')}  ->  TARGETS_CHAT_ID = {chat['id']}"
              + (f"   TARGETS_THREAD_ID = {msg['message_thread_id']}" if msg.get("message_thread_id") else ""))
    if not found:
        print(f"  Hech narsa topilmadi. @{me['username']} botini guruhga ADMIN qilib qo'shing,")
        print("  guruhga 'test' deb yozing va workflow'ni qayta ishga tushiring.")
    print("=" * 60)


# ----------------------------- State ----------------------------------------

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, separators=(",", ":"))


# ----------------------------- Ticker lists ---------------------------------

def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def yahoo_symbol(sym):
    return sym.strip().replace(".", "-").replace("/", "-")


BAD_NAMES = re.compile(r"\bWarrants?\b|\bRights?\b|\bUnits?\b|Preferred|Notes due|Debentures?", re.I)


def refresh_lists(state):
    if state.get("lists_time", 0) > time.time() - LIST_REFRESH_HOURS * 3600 and state.get("priority"):
        return
    names = {}
    try:
        sp = {}
        for row in csv.DictReader(io.StringIO(http_get(SP500_URL))):
            sym = yahoo_symbol(row["Symbol"])
            sp[sym] = row.get("Security") or sym
        nq = {}
        lines = http_get(NASDAQ_URL).splitlines()
        header = lines[0].split("|")
        for line in lines[1:]:
            if line.startswith("File Creation Time"):
                continue
            row = dict(zip(header, line.split("|")))
            if row.get("Test Issue") != "N" or row.get("ETF") != "N":
                continue
            name = row.get("Security Name", "")
            if BAD_NAMES.search(name):
                continue
            nq[yahoo_symbol(row["Symbol"])] = name.split(" - ")[0].strip()
    except Exception as exc:
        print(f"WARNING: could not refresh ticker lists ({exc}); using saved lists")
        return
    names.update(nq)
    names.update(sp)
    state["priority"] = sorted(sp)
    state["others"] = sorted(s for s in nq if s not in sp)
    state["names"] = names
    state["lists_time"] = time.time()
    print(f"Ticker lists: {len(sp)} S&P 500 + {len(state['others'])} other NASDAQ stocks")


def next_batch(state, key, size):
    tickers = state.get(key, [])
    if not tickers:
        return []
    start = state.setdefault("cursor", {}).get(key, 0) % len(tickers)
    batch = (tickers + tickers)[start:start + min(size, len(tickers))]
    state["cursor"][key] = (start + len(batch)) % len(tickers)
    return batch


# ----------------------------- Yahoo ----------------------------------------

def num(v):
    try:
        v = float(v)
        return v if v == v and v > 0 else None   # drop NaN / 0
    except (TypeError, ValueError):
        return None


def fetch(sym):
    try:
        df = yf.Ticker(sym).upgrades_downgrades
    except Exception as exc:
        if "No upgrade/downgrade history" in str(exc):
            return sym, [], None          # no analysts cover this stock
        return sym, None, str(exc)
    if df is None or len(df) == 0:
        return sym, [], None
    rows = []
    for r in df.reset_index().head(20).to_dict("records"):
        date = r.get("GradeDate")
        try:
            ts = int(date.timestamp())
        except Exception:
            continue
        rows.append({
            "ts": ts,
            "firm": str(r.get("Firm") or "").strip(),
            "to": str(r.get("ToGrade") or "").strip(),
            "from": str(r.get("FromGrade") or "").strip(),
            "action": str(r.get("Action") or "").strip().lower(),
            "new": num(r.get("currentPriceTarget")),
            "old": num(r.get("priorPriceTarget")),
        })
    return sym, rows, None


def row_key(r):
    return f"{r['firm']}|{r['to']}|{r['new']}"


def money(v):
    return f"${v:,.0f}" if v == int(v) else f"${v:,.2f}"


def format_post(sym, name, r):
    lines = [f"📊 <b>${html.escape(sym)}</b> — {html.escape(name)}"]
    action = ACTIONS.get(r["action"], r["action"] or "update")
    if r["action"] in ("up", "down") and r["from"]:
        grade = f"{html.escape(r['from'])} → {html.escape(r['to'])} ({action})"
    else:
        grade = f"{html.escape(r['to'])} ({action})" if r["to"] else action
    lines.append(f"🏦 {html.escape(r['firm'])}: {grade}")
    if r["new"] and r["old"] and r["old"] != r["new"]:
        pct = (r["new"] / r["old"] - 1) * 100
        lines.append(f"🎯 Target: {money(r['old'])} → {money(r['new'])} ({pct:+.1f}%)")
    elif r["new"]:
        lines.append(f"🎯 Target: {money(r['new'])}")
    lines.append(f"\n#PriceTarget #{re.sub(r'[^A-Za-z0-9]', '', sym)}")
    return "\n".join(lines)


def is_worth_posting(r):
    target_changed = r["new"] and r["old"] and r["new"] != r["old"]
    if ONLY_TARGET_CHANGES:
        return bool(target_changed)
    return bool(target_changed or r["action"] in ("up", "down", "init"))


# ----------------------------- Main -----------------------------------------

def scan(state):
    """Checks the next batch of tickers and adds new analyst actions to the queue."""
    last = state.setdefault("last", {})      # ticker -> [latest timestamp, [keys at that time]]
    queue = state.setdefault("queue", [])
    batch = next_batch(state, "priority", PRIORITY_BATCH) + next_batch(state, "others", OTHER_BATCH)
    print(f"Checking {len(batch)} tickers…")

    with ThreadPoolExecutor(WORKERS) as pool:
        results = list(pool.map(fetch, batch))

    errors = [(s, e) for s, rows, e in results if rows is None]
    now = time.time()
    queued = {q["id"] for q in queue}
    added = 0
    for sym, rows, err in results:
        if rows is None:
            continue
        if not rows:
            last.setdefault(sym, [0, []])
            continue
        newest = max(r["ts"] for r in rows)
        newest_keys = [row_key(r) for r in rows if r["ts"] == newest]
        if sym not in last:
            last[sym] = [newest, newest_keys]       # first time: remember, don't post
            continue
        prev_ts, prev_keys = last[sym]
        for r in rows:
            fresh = r["ts"] > prev_ts or (r["ts"] == prev_ts and row_key(r) not in prev_keys)
            item_id = f"{sym}|{r['ts']}|{row_key(r)}"
            if fresh and now - r["ts"] < MAX_AGE_DAYS * 86400 and is_worth_posting(r) and item_id not in queued:
                queue.append({"id": item_id, "ts": r["ts"], "sym": sym, "row": r})
                queued.add(item_id)
                added += 1
        if newest > prev_ts:
            last[sym] = [newest, newest_keys]
        elif newest == prev_ts:
            last[sym] = [newest, sorted(set(prev_keys) | set(newest_keys))]
    queue[:] = [q for q in queue if now - q["ts"] < QUEUE_MAX_AGE_HOURS * 3600]

    print(f"Yahoo: {len(results) - len(errors)} ok, {len(errors)} errors. New changes: {added}")
    for s, e in errors[:5]:
        print(f"  {s}: {e}")
    if batch and len(errors) == len(batch):
        save_state(state)
        raise SystemExit("Yahoo Finance hamma so'rovlarni rad etdi (bloklangan bo'lishi mumkin).")


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
    state = load_state()
    refresh_lists(state)
    scan(state)

    if not state.get("connected"):
        try:
            send_post("✅ <b>Price target bot connected.</b>\nAnalyst price target changes for S&amp;P 500 and NASDAQ stocks will appear here.")
        except Exception as exc:
            print(f"Telegram'ga yuborib bo'lmadi: {exc}")
            diagnose()
            save_state(state)
            raise SystemExit("Sozlamalarni yuqoridagi TEKSHIRUV natijasiga qarab to'g'rilang.")
        state["connected"] = True

    queue = state["queue"]
    now, start, end = window()
    total = len(state.get("priority", [])) + len(state.get("others", []))
    print(f"New York time {now:%a %H:%M}. Queue {len(queue)}. Tickers learned: {len(state['last'])}/{total}. "
          f"Posting window open: {in_window()}")
    if not in_window() and now.weekday() in SEND_WEEKDAYS and 0 < (start - now).total_seconds() <= 11 * 60:
        save_state(state)
        print(f"Waiting {(start - now).total_seconds():.0f}s for the posting window to open…")
        time.sleep((start - now).total_seconds())
    if not in_window():
        save_state(state)
        return

    # Inside the window: post the queue, scan more tickers, repeat until the window closes
    posted = errors = 0
    names = state.get("names", {})
    while in_window():
        queue.sort(key=lambda q: q["ts"])
        while queue and in_window():
            q = queue.pop(0)
            try:
                send_post(format_post(q["sym"], names.get(q["sym"], q["sym"]), q["row"]))
                posted += 1
            except Exception as exc:
                errors += 1
                print(f"ERROR sending {q['sym']}: {exc}")
            if (posted + errors) % 20 == 0:
                save_state(state)
            time.sleep(3)
        if not in_window():
            break
        save_state(state)
        now, start, end = window()
        time.sleep(max(1, min(RECHECK_SECONDS, (end - now).total_seconds())))
        if in_window():
            scan(state)

    save_state(state)
    print(f"Window closed. Posted {posted}, errors {errors}, left for tomorrow {len(queue)}")
    if errors and not posted:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
