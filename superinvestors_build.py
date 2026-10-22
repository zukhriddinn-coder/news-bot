"""
Trading Laboratory -> superinvestor portfolios (daily update)
Downloads the latest two 13F-HR filings of famous investors from SEC EDGAR (official, free),
turns CUSIP codes into tickers (OpenFIGI, free) and saves everything to superinvestors.json.
The request bot (superinvestors.py) only reads that file, so answers are fast.

SEC rules: at most 10 requests per second and a User-Agent with a contact e-mail
(set the SEC_CONTACT secret). https://www.sec.gov/os/accessing-edgar-data
"""

import json
import os
import re
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# SETTINGS (you can edit these)
# ---------------------------------------------------------------------------

# person, fund, SEC CIK number, a word that must be in the SEC name (safety check)
MANAGERS = [
    ("Warren Buffett", "Berkshire Hathaway", 1067983, "BERKSHIRE"),
    ("Bill Ackman", "Pershing Square", 1336528, "PERSHING SQUARE"),
    ("Michael Burry", "Scion Asset Management", 1649339, "SCION"),
    ("Li Lu", "Himalaya Capital", 1709323, "HIMALAYA"),
    ("Mohnish Pabrai", "Dalal Street", 1549575, "DALAL"),
    ("Stanley Druckenmiller", "Duquesne Family Office", 1536411, "DUQUESNE"),
    ("David Tepper", "Appaloosa", 1656456, "APPALOOSA"),
    ("Seth Klarman", "Baupost Group", 1061768, "BAUPOST"),
    ("David Einhorn", "Greenlight Capital", 1079114, "GREENLIGHT"),
    ("Dan Loeb", "Third Point", 1040273, "THIRD POINT"),
    ("Carl Icahn", "Icahn Capital", 921669, "ICAHN"),
    ("George Soros", "Soros Fund Management", 1029160, "SOROS"),
    ("Chase Coleman", "Tiger Global", 1167483, "TIGER GLOBAL"),
    ("Chuck Akre", "Akre Capital", 1112520, "AKRE"),
    ("Terry Smith", "Fundsmith", 1569205, "FUNDSMITH"),
    ("Tom Gayner", "Markel Group", 1096343, "MARKEL"),
    ("Howard Marks", "Oaktree Capital", 949509, "OAKTREE"),
    ("Andreas Halvorsen", "Viking Global", 1103804, "VIKING"),
    ("Philippe Laffont", "Coatue Management", 1135730, "COATUE"),
    ("Nelson Peltz", "Trian Fund Management", 1345471, "TRIAN"),
    ("Bruce Berkowitz", "Fairholme Capital", 1056831, "FAIRHOLME"),
    ("Stephen Mandel", "Lone Pine Capital", 1061165, "LONE PINE"),
    ("Tom Russo", "Gardner Russo & Quinn", 860643, "GARDNER RUSSO"),
    ("Ray Dalio", "Bridgewater Associates", 1350694, "BRIDGEWATER"),
    ("Charlie Munger (maktabi)", "Daily Journal", 783412, "DAILY JOURNAL"),
    ("ValueAct", "ValueAct Capital", 1418814, "VALUEACT"),
    ("Dan Davidowitz", "Polen Capital", 1034524, "POLEN"),
    ("Paul Singer", "Elliott Investment Management", 1791786, "ELLIOTT"),
]
OUT_FILE = "superinvestors.json"
CUSIP_FILE = "cusip_tickers.json"     # CUSIP -> ticker cache (kept between runs)

# ---------------------------------------------------------------------------

SEC_CONTACT = os.environ.get("SEC_CONTACT", "").strip()
OPENFIGI_KEY = os.environ.get("OPENFIGI_API_KEY", "").strip()
UA = f"TradingLaboratory research bot {SEC_CONTACT}".strip()
_last_sec = [0.0]


def sec_get(url, as_json=True):
    """GET from SEC with the required User-Agent, at most ~8 requests per second."""
    for attempt in range(4):
        wait = 0.125 - (time.time() - _last_sec[0])
        if wait > 0:
            time.sleep(wait)
        _last_sec[0] = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "identity"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
            return json.loads(data) if as_json else data
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and attempt < 3:
                time.sleep(5 * (attempt + 1))
                continue
            raise
    raise RuntimeError(f"SEC: too many retries for {url}")


def local(tag):
    return tag.rsplit("}", 1)[-1]


def child(el, name):
    for c in el:
        if local(c.tag) == name:
            return c
    return None


def text(el, *path):
    for name in path:
        el = child(el, name) if el is not None else None
    return (el.text or "").strip() if el is not None else ""


# ----------------------------- 13F filings ----------------------------------

def latest_13f(cik, keyword):
    """Returns (SEC name, [filing, filing]) for the two newest quarters (13F-HR only)."""
    sub = sec_get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
    name = sub.get("name", "")
    if keyword.upper() not in name.upper():
        raise ValueError(f"CIK {cik} belongs to '{name}', expected '{keyword}' — skipped")
    r = sub["filings"]["recent"]
    by_quarter = {}
    for form, acc, rep, filed in zip(r["form"], r["accessionNumber"], r["reportDate"], r["filingDate"]):
        if form != "13F-HR" or not rep:
            continue
        if rep not in by_quarter or filed > by_quarter[rep]["filed"]:
            by_quarter[rep] = {"accession": acc, "report_date": rep, "filed": filed}
    quarters = sorted(by_quarter.values(), key=lambda f: f["report_date"], reverse=True)[:2]
    return name, quarters


def info_table(cik, accession):
    folder = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}"
    index = sec_get(f"{folder}/index.json")
    xmls = [i for i in index.get("directory", {}).get("item", [])
            if i.get("name", "").lower().endswith(".xml") and i["name"].lower() != "primary_doc.xml"]
    if not xmls:
        raise ValueError(f"no information table in {accession}")
    best = max(xmls, key=lambda i: int(i.get("size") or 0))
    return sec_get(f"{folder}/{best['name']}", as_json=False)


def parse_holdings(xml_bytes, report_date):
    """Holdings grouped by CUSIP and kind (SH / PUT / CALL)."""
    root = ET.fromstring(xml_bytes)
    scale = 1000 if report_date < "2022-12-31" else 1        # values were in $1000s before 2023
    out = {}
    for row in root.iter():
        if local(row.tag) != "infoTable":
            continue
        cusip = text(row, "cusip").upper()
        if not cusip:
            continue
        kind = (text(row, "putCall") or "SH").upper()
        key = f"{cusip}:{kind}"
        h = out.setdefault(key, {"cusip": cusip, "kind": kind, "issuer": text(row, "nameOfIssuer"),
                                 "class": text(row, "titleOfClass"), "shares": 0.0, "value": 0.0})
        try:
            h["shares"] += float(text(row, "shrsOrPrnAmt", "sshPrnamt") or 0)
            h["value"] += float(text(row, "value") or 0) * scale
        except ValueError:
            pass
    return out


# ----------------------------- CUSIP -> ticker ------------------------------

def openfigi(cusips, cache):
    todo = [c for c in sorted(set(cusips)) if c not in cache]
    if not todo:
        return
    batch = 100 if OPENFIGI_KEY else 10
    pause = 0.3 if OPENFIGI_KEY else 2.6                # free limits: 25 requests / 6 s or / minute
    headers = {"Content-Type": "application/json"}
    if OPENFIGI_KEY:
        headers["X-OPENFIGI-APIKEY"] = OPENFIGI_KEY
    print(f"OpenFIGI: {len(todo)} new CUSIPs")
    for i in range(0, len(todo), batch):
        part = todo[i:i + batch]
        body = json.dumps([{"idType": "ID_CUSIP", "idValue": c} for c in part]).encode()
        for attempt in range(4):
            req = urllib.request.Request("https://api.openfigi.com/v3/mapping", data=body, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    res = json.load(resp)
                break
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(30 * (attempt + 1))
                    continue
                print(f"OpenFIGI HTTP {e.code}; stopping, the rest is matched by name")
                return
            except Exception as exc:
                print(f"OpenFIGI: {exc}; stopping, the rest is matched by name")
                return
        else:
            return
        for c, r in zip(part, res):
            data = r.get("data") or []
            us = [d for d in data if d.get("exchCode") == "US"] or data
            cache[c] = (us[0].get("ticker") or "").upper().replace("/", "-") if us else ""
        time.sleep(pause)


# ----------------------------- Main -----------------------------------------

def main():
    if not SEC_CONTACT or "@" not in SEC_CONTACT:
        raise SystemExit("SEC_CONTACT secret'ini qo'shing (SEC talabi: aloqa uchun e-mail).")
    try:
        with open(CUSIP_FILE, encoding="utf-8") as f:
            cusip_map = json.load(f)
    except Exception:
        cusip_map = {}

    managers, problems = [], []
    for person, fund, cik, keyword in MANAGERS:
        try:
            sec_name, quarters = latest_13f(cik, keyword)
            if not quarters:
                raise ValueError("no 13F-HR filings")
            snaps = []
            for q in quarters:
                holdings = parse_holdings(info_table(cik, q["accession"]), q["report_date"])
                snaps.append({**q, "total_value": sum(h["value"] for h in holdings.values() if h["kind"] == "SH"),
                              "holdings": holdings})
            managers.append({"person": person, "fund": fund, "cik": cik, "sec_name": sec_name,
                             "latest": snaps[0], "previous": snaps[1] if len(snaps) > 1 else None})
            print(f"OK  {person:24} {sec_name[:34]:34} {snaps[0]['report_date']}  "
                  f"{len(snaps[0]['holdings'])} positions")
        except Exception as exc:
            problems.append(f"{person}: {exc}")
            print(f"ERR {person}: {exc}")

    cusips = [h["cusip"] for m in managers for s in (m["latest"], m["previous"]) if s
              for h in s["holdings"].values()]
    openfigi(cusips, cusip_map)
    for m in managers:
        for s in (m["latest"], m["previous"]):
            if not s:
                continue
            for h in s["holdings"].values():
                h["ticker"] = cusip_map.get(h["cusip"], "")

    with open(CUSIP_FILE, "w", encoding="utf-8") as f:
        json.dump(cusip_map, f, separators=(",", ":"), sort_keys=True)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump({"updated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
                   "managers": managers, "problems": problems}, f, separators=(",", ":"))
    print(f"Saved {len(managers)} managers, {len(problems)} problems, {len(cusip_map)} CUSIPs known")
    if not managers:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
