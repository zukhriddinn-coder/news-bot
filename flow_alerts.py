"""
Trading Laboratory -> "Call oqimi" signal boti
OptionWhales (Flow Intelligence -> Abnormal Trades) lentasini har daqiqada tekshiradi va bozor narxidan
YUQORI strike'li (OTM), hajmi $300K+ bo'lgan har bir yangi CALL XARIDI uchun Telegram chatiga xabar yuboradi:
Signal kartasi uslubidagi rasm + matn (yoki faqat matn).

Ma'lumot: OptionWhales rasmiy API (https://api.optionwhales.io/v1/abnormal-trades/current), OW_API_KEY secret.
Ishlash joyi: GitHub Actions (flow_alerts.yml). Savdo kuni 09:30-16:15 ET oralig'ida ishlaydi.
Kalit va tokenlar faqat GitHub Secrets'da turadi, logga chiqarilmaydi.
"""

import glob
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

# ---------------------------------------------------------------------------
# SOZLAMALAR
# ---------------------------------------------------------------------------

MIN_PREMIUM = 300_000          # eng kichik hajm ($). 500_000 qilsangiz, faqat $500K+ keladi
MIN_CONFIDENCE = 0             # eng past "ishonch" (%). 70 qilsangiz, faqat BUYER 70%+ keladi
SKIP_MULTILEG = False          # True: ML (spread oyog'i) bitimlar yuborilmaydi
MIN_DTE = 0                    # muddatgacha eng kam kun (masalan 5: 0-4 kunlik lotereyalar chiqmaydi)
MAX_DTE = 100_000              # muddatgacha eng ko'p kun (masalan 45)
SEND_MODE = "photo"            # "photo" = rasm + matn, "text" = faqat matn
POLL_SECONDS = 60              # tekshirish oralig'i (Pro: daqiqasiga 60 so'rov)
MAX_PER_CYCLE = 12             # bitta tekshiruvda ko'pi bilan nechta xabar (qolgani keyingisida)
STARTUP_LOOKBACK_MIN = 10      # kun o'rtasida yoqilsa: faqat so'nggi N daqiqadagi bitimlar yuboriladi

# Aksiya bo'lmagan mahsulotlar (ETF, indeks, leveraged) va chiqarilgan tickerlar yuborilmaydi
EXCLUDE = set("""
SPY QQQ IWM DIA SPX SPXW NDX RUT VIX XSP SMH SOXX SOXL SOXS TQQQ SQQQ UVXY VXX TLT HYG LQD GLD SLV USO UNG
XLE XLF XLK XLV XLI XLY XLP XLU XLB XLRE XLC KRE XBI ARKK ARKG EEM EFA FXI KWEB EWZ EWY EWJ EWW INDA GDX GDXJ
TSLL TSLQ NVDL NVDS NVDX AMDL MSTU MSTX MSTZ CONL BITO BITX IBIT ETHA ETHU FBTC GBTC TNA TZA LABU LABD SPXL
SPXS SPXU UPRO SDS SH PSQ QID SSO QLD TMF TBT BOIL KOLD UCO SCO YINN YANG FAS FAZ NUGT DUST JNUG JDST AGQ ZSL
SVXY UVIX VIXY IYR VNQ XOP OIH XHB ITB XRT JETS TAN ICLN URA COPX SIL REMX USHY JNK SJNK BND AGG IEF SHY SGOV
BIL VOO VTI IVV RSP MDY IJR SCHD JEPI JEPQ QYLD TSLT PLTU GGLL AAPU METU AMZU MSFU CRWL HOOX DXYZ IGV SKUU
SMCX DRAM
PCG GME TEM
""".split())

# ---------------------------------------------------------------------------

OW_KEY = os.environ.get("OW_API_KEY", "").strip()
BOT_TOKEN = os.environ.get("FLOW_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("FLOW_CHAT_ID", "").strip()
THREAD_ID = os.environ.get("FLOW_THREAD_ID", "").strip()
SEGMENT = (os.environ.get("SEGMENT", "") or "all").strip()      # "1" = 12:45 ET gacha, "2" = yopilishgacha
TEST = (os.environ.get("TEST", "") or "").strip().lower() in ("1", "true", "yes")
STATE_FILE = os.environ.get("STATE_FILE", "flow_state.json")

OW_URL = "https://api.optionwhales.io/v1/abnormal-trades/current"
NY = ZoneInfo("America/New_York")
TAS = ZoneInfo("Asia/Tashkent")
MONTHS = ["yan", "fev", "mar", "apr", "may", "iyn", "iyl", "avg", "sen", "okt", "noy", "dek"]


def log(*a):
    print(datetime.now(NY).strftime("%H:%M:%S ET"), *a, flush=True)


# ----------------------------- OptionWhales --------------------------------

def fetch_trades():
    """Bugungi g'ayrioddiy bitimlar lentasi: (session, [trade, ...]) yoki (None, None) xato bo'lsa."""
    for attempt in range(3):
        try:
            r = requests.get(OW_URL, params={"limit": 5000}, timeout=40,
                             headers={"X-API-Key": OW_KEY, "Accept": "application/json",
                                      "User-Agent": "TradingLab-flow/1.0"})
            if r.status_code == 429:
                time.sleep(float(r.headers.get("Retry-After") or 10) + 1)
                continue
            if r.status_code != 200:
                log(f"OptionWhales HTTP {r.status_code}")
                return None, None
            j = r.json()
            trades = j.get("trades") if isinstance(j, dict) else j
            return (j.get("session") if isinstance(j, dict) else None), (trades or [])
        except Exception as e:
            log(f"OptionWhales: {type(e).__name__}")
            time.sleep(3)
    return None, None


def parse(t):
    """API yozuvini bot ishlatadigan oddiy ko'rinishga keltiradi; kerakli maydon bo'lmasa None."""
    m = re.search(r"([A-Z.]+?)(\d{2})(\d{2})(\d{2})([CP])(\d{8})$", str(t.get("option_ticker") or ""))
    if not m:
        return None
    ticker = str(t.get("ticker") or m.group(1)).upper()
    try:
        exp = datetime(2000 + int(m.group(2)), int(m.group(3)), int(m.group(4))).date()
        strike = int(m.group(6)) / 1000
        spot = float(t.get("spot_price") or 0)
        prem = float(t.get("total_premium") or 0)
        ts = datetime.fromisoformat(str(t.get("timestamp")).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    conf = t.get("direction_confidence", t.get("confidence"))
    conf = round(float(conf) * 100) if conf is not None else None
    contracts = t.get("total_contracts") or t.get("contract_size") or t.get("trade_size") or 0
    return {
        "id": str(t.get("event_id") or f"{t.get('option_ticker')}|{t.get('timestamp')}|{round(prem)}"),
        "ticker": ticker, "type": m.group(5), "strike": strike, "exp": exp, "spot": spot, "premium": prem,
        "contracts": int(contracts), "conf": conf, "direction": str(t.get("direction") or "").lower(),
        "order": str(t.get("order_type") or "").lower() or "-", "ml": bool(t.get("multileg")), "ts": ts,
    }


def wanted(x, today):
    """Filtr: aksiya, CALL XARIDI, hajm >= MIN_PREMIUM, strike bozor narxidan yuqori (OTM)."""
    if x["type"] != "C" or x["direction"] != "buy":
        return False
    if x["ticker"] in EXCLUDE or x["premium"] < MIN_PREMIUM:
        return False
    if x["spot"] <= 0 or x["strike"] <= x["spot"]:
        return False
    if x["conf"] is not None and x["conf"] < MIN_CONFIDENCE:
        return False
    if SKIP_MULTILEG and x["ml"]:
        return False
    return MIN_DTE <= (x["exp"] - today).days <= MAX_DTE


# ----------------------------- Matn ----------------------------------------

def money(v):
    return f"${v / 1e6:.1f}M" if v >= 1e6 else f"${v / 1e3:.0f}K"


def fnum(v):
    s = f"{v:,.2f}"
    return s[:-3] if s.endswith(".00") else s


def exp_label(d):
    return f"{d.day}-{MONTHS[d.month - 1]}-{d.year}"


def fields(x):
    today = x["ts"].astimezone(NY).date()
    return {
        "strike": f"${fnum(x['strike'])}C", "exp": exp_label(x["exp"]), "dte": (x["exp"] - today).days,
        "spot": f"${x['spot']:,.2f}", "otm": f"+{(x['strike'] / x['spot'] - 1) * 100:.1f}%",
        "prem": money(x["premium"]), "contracts": f"{x['contracts']:,}",
        "conf": f"{x['conf']}%" if x["conf"] is not None else "-",
        "et": x["ts"].astimezone(NY).strftime("%H:%M"), "tas": x["ts"].astimezone(TAS).strftime("%H:%M"),
    }


def caption(x):
    f = fields(x)
    big = "⚡ " if x["premium"] >= 1_000_000 else ""
    return (f"🟢 <b>${x['ticker']}</b> · CALL xaridi {big}\n"
            f"Strike: <b>{f['strike']}</b> · Muddat: <b>{f['exp']}</b> ({f['dte']} kun)\n"
            f"Narx: {f['spot']} · OTM: <b>{f['otm']}</b>\n"
            f"Hajm: <b>{f['prem']}</b> · Kontrakt: {f['contracts']}\n"
            f"Ishonch: {f['conf']} · Order: {x['order']}{' · ML' if x['ml'] else ''}\n"
            f"Vaqt: {f['et']} ET ({f['tas']} Toshkent)\n\n"
            f"<i>Bu ma'lumot, savdo signali emas. Investitsion maslahat emas.</i>")


# ----------------------------- Rasm (Signal kartasi uslubi) ----------------

def font(size, bold=True):
    from PIL import ImageFont
    names = (["Inter-Bold", "Inter-SemiBold", "DejaVuSans-Bold", "LiberationSans-Bold"] if bold
             else ["Inter-Regular", "Inter-Medium", "DejaVuSans.", "LiberationSans-Regular"])
    for n in names:
        for p in glob.glob("/usr/share/fonts/**/*", recursive=True):
            if n in os.path.basename(p) and p.lower().endswith((".ttf", ".otf")):
                try:
                    return ImageFont.truetype(p, size)
                except OSError:
                    pass
    return ImageFont.load_default()


def card(x):
    """1080x1080 PNG: to'q yashil fon, laym urg'u (Signal va Portfel kartasi uslubi)."""
    from PIL import Image, ImageDraw
    f = fields(x)
    W = H = 1080
    LIME, WHITE, MUTED = (184, 241, 90), (255, 255, 255), (150, 180, 162)
    img = Image.new("RGB", (W, H))
    px = img.load()
    for y in range(H):                                    # vertikal gradient
        k = y / H
        row = (int(22 - 14 * k), int(82 - 52 * k), int(52 - 34 * k))
        for xx in range(W):
            px[xx, y] = row
    d = ImageDraw.Draw(img, "RGBA")
    for g in range(0, W, 60):                             # nozik to'r
        d.line([(g, 0), (g, H)], fill=(255, 255, 255, 7))
        d.line([(0, g), (W, g)], fill=(255, 255, 255, 7))

    M = 64
    # sarlavha
    d.rounded_rectangle([M, 60, M + 40, 100], 10, outline=LIME, width=3)
    d.rounded_rectangle([M + 12, 72, M + 28, 88], 4, fill=LIME)
    d.text((M + 58, 80), "TRADING LABORATORY", font=font(26), fill=WHITE, anchor="lm")
    badge = "CALL OQIMI"
    bw = d.textlength(badge, font=font(22)) + 44
    d.rounded_rectangle([W - M - bw, 58, W - M, 102], 22, fill=LIME)
    d.text((W - M - bw / 2, 80), badge, font=font(22), fill=(11, 46, 34), anchor="mm")

    d.text((M, 150), f"KIT BITIMI · {f['et']} ET · {f['tas']} TOSHKENT", font=font(22), fill=LIME, anchor="lm")

    # ticker
    tk = x["ticker"]
    size = 190 if len(tk) <= 4 else 150
    d.text((M - 6, 285), tk, font=font(size), fill=WHITE, anchor="lm")
    tw = d.textlength(tk, font=font(size))
    d.text((M + tw + 22, 330), "call", font=font(54, bold=False), fill=LIME, anchor="lm")
    d.text((M, 410), f"{f['strike']}  ·  {f['exp']}  ·  {f['dte']} kun", font=font(34, bold=False),
           fill=(205, 225, 212), anchor="lm")

    # asosiy plitka: hajm
    d.rounded_rectangle([M, 470, W - M, 610], 30, fill=LIME)
    d.text((M + 40, 520), "OPSION HAJMI", font=font(24), fill=(11, 46, 34), anchor="lm")
    d.text((M + 40, 565), f"{f['contracts']} kontrakt · {x['order']}", font=font(26, bold=False),
           fill=(30, 70, 50), anchor="lm")
    d.text((W - M - 40, 540), f['prem'], font=font(84), fill=(11, 46, 34), anchor="rm")

    # 2x2 plitkalar
    tiles = [("STRIKE", f["strike"]), ("AKSIYA NARXI", f["spot"]),
             ("NARXDAN YUQORI (OTM)", f["otm"]), ("ISHONCH (BUYER)", f["conf"])]
    tw_, th_, gap = (W - 2 * M - 24) // 2, 150, 24
    for i, (lab, val) in enumerate(tiles):
        x0 = M + (i % 2) * (tw_ + gap)
        y0 = 640 + (i // 2) * (th_ + gap)
        d.rounded_rectangle([x0, y0, x0 + tw_, y0 + th_], 26, fill=(255, 255, 255, 16),
                            outline=(255, 255, 255, 36), width=2)
        d.text((x0 + 32, y0 + 44), lab, font=font(21), fill=MUTED, anchor="lm")
        d.text((x0 + 32, y0 + 102), val, font=font(54), fill=LIME if i == 2 else WHITE, anchor="lm")

    if x["ml"]:
        d.rounded_rectangle([W - M - 250, 388, W - M, 432], 22, outline=(255, 190, 90), width=2)
        d.text((W - M - 125, 410), "ML · spread oyog'i", font=font(20), fill=(255, 205, 120), anchor="mm")

    d.text((M, H - 48), "Trading Laboratory · OptionWhales oqimi", font=font(22, bold=False), fill=MUTED,
           anchor="lm")
    d.text((W - M, H - 48), "Investitsion maslahat emas", font=font(22, bold=False), fill=MUTED, anchor="rm")
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


# ----------------------------- Telegram ------------------------------------

def tg(method, data, files=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    for attempt in range(4):
        try:
            r = requests.post(url, data=data, files=files, timeout=60)
            j = r.json()
            if j.get("ok"):
                return True
            if r.status_code == 429:
                time.sleep(int(j.get("parameters", {}).get("retry_after", 5)) + 1)
                continue
            log(f"Telegram {method}: {j.get('description')}")
            return False
        except Exception as e:
            log(f"Telegram {method}: {type(e).__name__}")
            time.sleep(3)
    return False


def send(x):
    base = {"chat_id": CHAT_ID, "parse_mode": "HTML"}
    if THREAD_ID:
        base["message_thread_id"] = THREAD_ID
    text = caption(x)
    if SEND_MODE == "photo":
        try:
            png = card(x)
            if tg("sendPhoto", {**base, "caption": text}, {"photo": ("call.png", png, "image/png")}):
                return True
        except Exception as e:
            log(f"Rasm chizilmadi ({type(e).__name__}), matn yuboriladi")
    return tg("sendMessage", {**base, "text": text, "disable_web_page_preview": "true"})


# ----------------------------- Holat (qaysi bitimlar yuborilgan) -----------

def load_state(session):
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            s = json.load(fh)
        if s.get("session") == session:
            return set(s.get("seen", [])), True
    except (OSError, ValueError):
        pass
    return set(), False


def save_state(session, seen):
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump({"session": session, "seen": sorted(seen)}, fh)


# ----------------------------- Asosiy sikl ---------------------------------

def sample():
    now = datetime.now(timezone.utc)
    return {"id": "test", "ticker": "TEST", "type": "C", "strike": 175.0, "exp": (now + timedelta(days=56)).date(),
            "spot": 136.09, "premium": 1_400_000, "contracts": 8357, "conf": 77, "direction": "buy",
            "order": "single", "ml": False, "ts": now}


def main():
    missing = [n for n, v in (("OW_API_KEY", OW_KEY), ("FLOW_BOT_TOKEN", BOT_TOKEN), ("FLOW_CHAT_ID", CHAT_ID)) if not v]
    if missing and not (TEST and "OW_API_KEY" in missing and len(missing) == 1):
        sys.exit("XATO: secret topilmadi: " + ", ".join(missing))
    if TEST:
        ok = send(sample())
        sys.exit(0 if ok else "XATO: sinov xabari yuborilmadi (token, chat ID yoki bot huquqlarini tekshiring)")

    now = datetime.now(NY)
    if now.weekday() >= 5:
        log("Dam olish kuni, chiqildi"); return
    open_t = now.replace(hour=9, minute=30, second=0, microsecond=0)
    end_t = now.replace(hour=16, minute=15, second=0, microsecond=0)
    split_t = now.replace(hour=12, minute=45, second=0, microsecond=0)
    if SEGMENT == "1":
        end_t = split_t
    if now >= end_t:
        log("Bu qism uchun vaqt o'tgan, chiqildi"); return
    if now < open_t:
        log(f"Bozor ochilishi kutilmoqda ({int((open_t - now).total_seconds() // 60)} daqiqa)")
        time.sleep((open_t - now).total_seconds())

    today = datetime.now(NY).date()
    session = today.isoformat()
    seen, had_state = load_state(session)
    first = not had_state
    sent = 0
    log(f"Boshlandi: qism {SEGMENT}, yodda {len(seen)} ta bitim")

    while datetime.now(NY) < end_t:
        t0 = time.time()
        api_session, trades = fetch_trades()
        if trades is not None and (api_session in (None, session)):
            fresh = []
            for raw in trades:
                x = parse(raw)
                if not x or x["id"] in seen or x["ts"].astimezone(NY).date() != today:
                    continue
                if not wanted(x, today):
                    seen.add(x["id"])
                    continue
                fresh.append(x)
            if first:                                      # kun o'rtasida yoqildi: eskilarini yubormaymiz
                cut = datetime.now(timezone.utc) - timedelta(minutes=STARTUP_LOOKBACK_MIN)
                for x in [x for x in fresh if x["ts"] < cut]:
                    seen.add(x["id"])
                fresh = [x for x in fresh if x["ts"] >= cut]
                first = False
            fresh.sort(key=lambda x: x["ts"])
            for x in fresh[:MAX_PER_CYCLE]:
                if send(x):
                    seen.add(x["id"])
                    sent += 1
                    log(f"Yuborildi: {x['ticker']} {x['strike']}C {x['exp']} {money(x['premium'])}")
                    time.sleep(3.2)                        # Telegram: guruhga daqiqasiga ~20 xabar
            save_state(session, seen)
        time.sleep(max(5, POLL_SECONDS - (time.time() - t0)))

    save_state(session, seen)
    log(f"Tugadi: {sent} ta xabar yuborildi")


if __name__ == "__main__":
    main()
