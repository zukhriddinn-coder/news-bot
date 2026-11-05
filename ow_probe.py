"""
Trading Laboratory -> OptionWhales API probe (one-off test, not a bot)
Calls the OptionWhales endpoints we may use for one ticker and prints, for each one, the HTTP status and the
SHAPE of the answer (field names, types, the first item of lists, short sample values) into the GitHub Actions
log. It also prints the account tier and usage. The API key is read from the OW_API_KEY secret and never printed.
Run: Actions -> OptionWhales probe -> Run workflow (ticker, e.g. NVDA). Then send the log to Claude.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

KEY = os.environ.get("OW_API_KEY", "").strip()
TICKER = (os.environ.get("TICKER", "") or "NVDA").strip().upper().lstrip("$")
BASE = "https://api.optionwhales.io/v1"


def ny_today():
    utc = datetime.now(timezone.utc)
    # US DST: 2nd Sunday of March 07:00 UTC -> 1st Sunday of November 06:00 UTC
    def nth_sunday(y, m, n):
        d = datetime(y, m, 1, tzinfo=timezone.utc)
        d += timedelta(days=(6 - d.weekday()) % 7)
        return d + timedelta(weeks=n - 1)
    start = nth_sunday(utc.year, 3, 2).replace(hour=7)
    end = nth_sunday(utc.year, 11, 1).replace(hour=6)
    return (utc - timedelta(hours=4 if start <= utc < end else 5)).date()


def last_sessions(n=3):
    d, out = ny_today(), []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= timedelta(days=1)
    return out


def get(path, **params):
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={"X-API-Key": KEY, "Accept": "application/json",
                                               "User-Agent": "TradingLab-probe/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            hdr = {k: v for k, v in r.headers.items() if k.lower().startswith("x-ratelimit")}
            return r.status, json.loads(r.read().decode("utf-8") or "null"), hdr
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        return e.code, body, {}
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}", {}


def shape(x, depth=0, max_depth=5):
    """Field names and types with short samples; lists show their length and the first item."""
    pad = "  " * depth
    if depth > max_depth:
        return pad + "…"
    if isinstance(x, dict):
        lines = []
        for k, v in list(x.items())[:40]:
            if isinstance(v, (dict, list)):
                lines.append(f"{pad}{k}: {type(v).__name__}" + (f"[{len(v)}]" if isinstance(v, list) else ""))
                lines.append(shape(v, depth + 1, max_depth))
            else:
                s = json.dumps(v)
                lines.append(f"{pad}{k}: {s[:60]}")
        if len(x) > 40:
            lines.append(f"{pad}… (+{len(x) - 40} fields)")
        return "\n".join(l for l in lines if l)
    if isinstance(x, list):
        if not x:
            return pad + "(empty list)"
        return pad + "[0] ->\n" + shape(x[0], depth + 1, max_depth)
    return pad + json.dumps(x)[:60]


def main():
    if not KEY:
        raise SystemExit("XATO: OW_API_KEY secret topilmadi — Settings → Secrets and variables → Actions")
    s0, s1, s2 = last_sessions(3)
    t = TICKER
    calls = [
        ("/account/usage", {}),
        ("/flow/current", {}),
        (f"/flow/{s0}", {}), (f"/flow/{s0}/{t}", {}), (f"/flow/{s1}/{t}", {}),
        ("/momentum/rankings", {}), (f"/momentum/{t}/history", {"sessions": 5}),
        ("/abnormal-trades/current", {"limit": 5}), (f"/abnormal-trades/{s1}", {"limit": 5}),
        (f"/gex/{s0}/{t}/levels", {}), (f"/gex/{s1}/{t}/levels", {}), (f"/gex/{s1}/{t}", {}),
        (f"/volatility/{t}/iv-rank", {}), (f"/volatility/{t}/stats", {}), (f"/volatility/{t}/term-structure", {}),
        (f"/oi/{t}/latest", {}), (f"/oi/{t}/change", {}),
        (f"/directional/{t}/history", {}), ("/directional/current", {"max_display": 10}),
        (f"/tide/{t}", {}),
        ("/options-flow/tape", {"ticker": t, "limit": 3}),
        ("/darkpool/recent", {"ticker": t, "limit": 3}),
        (f"/short/{t}/interest", {}),
        (f"/earnings/{s1}/{t}", {}),
    ]
    print(f"Ticker {t}, sessions {s0} / {s1} / {s2}\n")
    for path, params in calls:
        code, body, hdr = get(path, **params)
        print("=" * 100)
        print(f"GET {path} {params or ''}  ->  HTTP {code}  {hdr or ''}")
        if isinstance(body, (dict, list)):
            print(shape(body))
        else:
            print(str(body)[:300])
        time.sleep(7)                      # Free: 10 calls a minute; stay under it
    print("\nTayyor. Shu logni Claude'ga yuboring.")


if __name__ == "__main__":
    main()
