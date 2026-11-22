"""
Trading Laboratory -> "Call oqimi" signal boti
OptionWhales (Flow Intelligence -> Abnormal Trades) lentasini har daqiqada tekshiradi va bozor narxidan
YUQORI strike'li (OTM), hajmi $300K+ bo'lgan har bir yangi CALL XARIDI uchun Telegram chatiga xabar yuboradi:
Signal kartasi uslubidagi rasm (sozlamada: rasm + matn yoki faqat matn).

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
SEND_MODE = "photo"            # "photo" = faqat rasm, "photo_text" = rasm + matn, "text" = faqat matn
POLL_SECONDS = 60              # tekshirish oralig'i (Pro: daqiqasiga 60 so'rov)
MAX_PER_CYCLE = 12             # bitta tekshiruvda ko'pi bilan nechta xabar (qolgani keyingisida)
STARTUP_LOOKBACK_MIN = 10      # kun o'rtasida yoqilsa: faqat so'nggi N daqiqadagi bitimlar yuboriladi

# Order turi ranglari (kartada doim shunday chiqadi)
ORDER_STYLE = {
    "sweep": ("Sweep", (255, 214, 10)),     # sariq
    "burst": ("Burst", (255, 146, 43)),     # to'q sariq (orange)
    "single": ("Single", (255, 255, 255)),  # oq
}

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
REPLAY_DAY = (os.environ.get("REPLAY_DAY", "") or "").strip()          # masalan 2026-11-20: o'tgan kunni qayta chiqarish
REPLAY_TICKER = (os.environ.get("REPLAY_TICKER", "") or "").strip().upper().lstrip("$")
REPLAY_MAX = 40                # qayta chiqarishda ko'pi bilan nechta xabar
STATE_FILE = os.environ.get("STATE_FILE", "flow_state.json")

OW_URL = "https://api.optionwhales.io/v1/abnormal-trades/"
NY = ZoneInfo("America/New_York")
TAS = ZoneInfo("Asia/Tashkent")
MONTHS = ["yan", "fev", "mar", "apr", "may", "iyn", "iyl", "avg", "sen", "okt", "noy", "dek"]


def log(*a):
    print(datetime.now(NY).strftime("%H:%M:%S ET"), *a, flush=True)


# ----------------------------- OptionWhales --------------------------------

def fetch_trades(day="current"):
    """Bugungi g'ayrioddiy bitimlar lentasi: (session, [trade, ...]) yoki (None, None) xato bo'lsa."""
    for attempt in range(3):
        try:
            r = requests.get(OW_URL + day, params={"limit": 5000}, timeout=40,
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

_FONTS = {"bold": ["Inter-Bold", "Inter-SemiBold", "DejaVuSans-Bold", "LiberationSans-Bold"],
          "regular": ["Inter-Regular", "Inter-Medium", "DejaVuSans.", "LiberationSans-Regular"],
          "serif": ["LiberationSerif-Italic", "DejaVuSerif-Italic", "FreeSerifItalic", "Inter-Italic"]}
_font_paths = {}


def font(size, kind="bold"):
    from PIL import ImageFont
    if kind is True:
        kind = "bold"
    elif kind is False:
        kind = "regular"
    if kind not in _font_paths:
        files = glob.glob("/usr/share/fonts/**/*", recursive=True)
        _font_paths[kind] = next((p for n in _FONTS[kind] for p in files
                                  if n in os.path.basename(p) and p.lower().endswith((".ttf", ".otf"))), None)
    try:
        return ImageFont.truetype(_font_paths[kind], size)
    except (OSError, TypeError, AttributeError):
        return ImageFont.load_default()


def card(x):
    """1080x1350 PNG, Signal va Portfel kartasi uslubida (2x chizilib, kichraytiriladi: silliq qirralar)."""
    from PIL import Image, ImageDraw, ImageFilter
    f = fields(x)
    S, W, H, M = 2, 1080, 1350, 70
    LIME, WHITE, DARK = (184, 241, 90), (255, 255, 255), (10, 40, 28)
    MUTED, SOFT, RED = (140, 172, 152), (200, 222, 208), (255, 122, 104)

    img = Image.new("RGB", (W * S, H * S))
    d0 = ImageDraw.Draw(img)
    for y in range(H * S):                                 # fon: yuqoridan pastga to'q yashil
        k = y / (H * S)
        d0.line([(0, y), (W * S, y)], fill=(int(20 - 13 * k), int(74 - 50 * k), int(46 - 32 * k)))
    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))       # yuqoridagi yumshoq yorug'lik
    ImageDraw.Draw(glow).ellipse([200 * S, -420 * S, 1250 * S, 430 * S], fill=(60, 170, 95, 105))
    img.paste(glow.filter(ImageFilter.GaussianBlur(150 * S)), (0, 0), glow.filter(ImageFilter.GaussianBlur(150 * S)))
    d = ImageDraw.Draw(img, "RGBA")
    for g in range(0, H, 54):                              # nozik to'r
        d.line([(g * S, 0), (g * S, H * S)], fill=(255, 255, 255, 7), width=S)
        d.line([(0, g * S), (W * S, g * S)], fill=(255, 255, 255, 7), width=S)

    def R(box, r, **kw):
        if "width" in kw:
            kw["width"] *= S
        d.rounded_rectangle([v * S for v in box], r * S, **kw)

    def T(xy, text, size, kind="bold", fill=WHITE, anchor="lm", track=0):
        fnt = font(size * S, kind)
        if not track:
            d.text((xy[0] * S, xy[1] * S), text, font=fnt, fill=fill, anchor=anchor)
            return d.textlength(text, font=fnt) / S
        total = sum(d.textlength(c, font=fnt) for c in text) / S + track * (len(text) - 1)
        cx = xy[0] - (total if anchor[0] == "r" else total / 2 if anchor[0] == "m" else 0)
        for c in text:
            d.text((cx * S, xy[1] * S), c, font=fnt, fill=fill, anchor="l" + anchor[1])
            cx += d.textlength(c, font=fnt) / S + track
        return total

    def glass(box, r=28, tint=None):
        R(box, r, fill=tint or (255, 255, 255, 15), outline=(255, 255, 255, 34), width=1)

    def lime_tile(box, r, c1=(226, 248, 150), c2=(112, 204, 122)):
        x0, y0, x1, y1 = [v * S for v in box]
        grad = Image.new("RGB", (x1 - x0, y1 - y0))
        gd = ImageDraw.Draw(grad)
        for i in range(x1 - x0):
            k = i / (x1 - x0)
            gd.line([(i, 0), (i, y1 - y0)], fill=tuple(int(c1[j] + (c2[j] - c1[j]) * k) for j in range(3)))
        mask = Image.new("L", grad.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, x1 - x0 - 1, y1 - y0 - 1], r * S, fill=255)
        img.paste(grad, (x0, y0), mask)

    # --- sarlavha
    R([M, 70, M + 40, 110], 11, outline=LIME, width=2)
    R([M + 12, 82, M + 28, 98], 4, fill=LIME)
    T((M + 56, 90), "TRADING LABORATORY", 22, track=3.2)
    ocol = ORDER_STYLE.get(x["order"], ("", WHITE))[1]
    tag = "ML · spread" if x["ml"] else {"sweep": "Sweep", "burst": "Burst", "single": "Blok"}.get(x["order"], x["order"])
    tw = d.textlength(tag, font=font(21 * S, "regular")) / S
    R([W - M - tw - 150, 64, W - M, 116], 26, fill=(255, 255, 255, 14), outline=(255, 255, 255, 40), width=1)
    R([W - M - tw - 142, 72, W - M - tw - 38, 108], 18, fill=LIME)
    T((W - M - tw - 90, 90), "CALL", 18, fill=DARK, anchor="mm", track=1.5)
    T((W - M - 22, 90), tag, 21, "regular", fill=(255, 205, 120) if x["ml"] else ocol, anchor="rm")

    T((M, 176), f"KIT BITIMI · {f['et']} ET · {f['tas']} TOSHKENT", 20, fill=(150, 214, 110), track=4)

    # --- ticker
    tk = x["ticker"]
    size = 200 if len(tk) <= 4 else 164 if len(tk) == 5 else 140
    wt = T((M - 8, 292), tk, size)
    T((M + wt + 14, 332), "call", 62, "serif", fill=LIME)
    T((M, 412), f"Muddat {f['exp']}  ·  {f['dte']} kun qoldi", 28, "regular", fill=SOFT)

    # --- chap ustun
    LX1 = 648
    lime_tile([M, 462, LX1, 580], 30)
    T((M + 30, 505), "OPSION HAJMI", 17, fill=DARK, track=3)
    T((M + 30, 540), f"{f['contracts']} kontrakt", 21, "regular", fill=(34, 78, 54))
    T((LX1 - 30, 521), f["prem"], 66, fill=DARK, anchor="rm")
    rows = [("STRIKE", "Opsion narxi darajasi", f["strike"], WHITE),
            ("AKSIYA NARXI", "Bitim paytida", f["spot"], WHITE),
            ("ISHONCH", "Xaridor tashabbusi", f["conf"], WHITE)]
    for i, (lab, sub, val, col) in enumerate(rows):
        y0 = 600 + i * 128
        glass([M, y0, LX1, y0 + 110])
        T((M + 30, y0 + 40), lab, 17, fill=MUTED, track=3)
        T((M + 30, y0 + 74), sub, 20, "regular", fill=MUTED)
        T((LX1 - 30, y0 + 55), val, 46, fill=col, anchor="rm")

    # --- o'ng: narxdan strike'gacha shkala
    PX0, PX1 = 680, W - M
    glass([PX0, 462, PX1, 966], 30)
    lx, yt, yb = PX0 + 62, 540, 890
    for y in range(yt * S, yb * S):
        k = (y - yt * S) / ((yb - yt) * S)
        d.line([((lx - 6) * S, y), ((lx + 6) * S, y)],
               fill=(int(184 - 110 * k), int(241 - 70 * k), int(90 + 50 * k)))
    d.ellipse([(lx - 15) * S, (yt - 15) * S, (lx + 15) * S, (yt + 15) * S], fill=LIME, outline=DARK, width=4 * S)
    d.ellipse([(lx - 20) * S, (yb - 20) * S, (lx + 20) * S, (yb + 20) * S], fill=(255, 255, 255, 40))
    d.ellipse([(lx - 13) * S, (yb - 13) * S, (lx + 13) * S, (yb + 13) * S], fill=WHITE, outline=DARK, width=4 * S)
    T((lx + 44, yt - 20), "STRIKE", 17, fill=MUTED, track=3)
    T((lx + 44, yt + 20), f["strike"], 38)
    T((lx + 44, yb - 22), "NARX", 17, fill=MUTED, track=3)
    T((lx + 44, yb + 18), f["spot"], 34, fill=LIME)
    ym = (yt + yb) // 2
    R([lx + 34, ym - 34, PX1 - 26, ym + 34], 18, fill=(184, 241, 90, 34), outline=(184, 241, 90, 150), width=1)
    T(((lx + 34 + PX1 - 26) / 2, ym), "↑ " + f["otm"], 32, fill=LIME, anchor="mm")

    # --- 3 ta ko'rsatkich
    stats = [(f["otm"], "NARXDAN YUQORI (OTM)"), (f"{f['dte']} kun", "MUDDATGACHA"), (x["order"], "ORDER TURI")]
    sw = (W - 2 * M - 2 * 20) / 3
    for i, (val, lab) in enumerate(stats):
        x0 = M + i * (sw + 20)
        glass([x0, 990, x0 + sw, 1096], 24)
        T((x0 + 26, 1028), val, 36, fill=ocol if i == 2 else WHITE)
        T((x0 + 26, 1068), lab, 14, fill=MUTED, track=2.2)

    # --- ogohlantirish
    lime_tile([M, 1122, W - M, 1250], 30, (232, 250, 168), (176, 240, 96))
    R([M + 28, 1152, M + 96, 1220], 18, fill=DARK)
    T((M + 62, 1186), "!", 40, fill=LIME, anchor="mm")
    T((M + 122, 1164), "Bu ma'lumot, savdo signali emas", 30, fill=DARK)
    T((M + 122, 1208), "Yirik bitim o'sishni kafolatlamaydi. Qaror oldidan tahlil qiling.", 20, "regular", fill=(30, 70, 48))

    T((M, 1302), "Trading Laboratory · yopiq kanal", 20, "regular", fill=MUTED)
    T((W - M, 1302), "Investitsion maslahat emas", 20, "regular", fill=MUTED, anchor="rm")

    buf = io.BytesIO()
    img.resize((W, H), Image.LANCZOS).save(buf, "PNG", optimize=True)
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
    if SEND_MODE in ("photo", "photo_text"):
        try:
            png = card(x)
            data = {**base, "caption": text} if SEND_MODE == "photo_text" else base
            if tg("sendPhoto", data, {"photo": ("call.png", png, "image/png")}):
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


def replay():
    """Sinov: o'tgan savdo kunining mos bitimlarini (ixtiyoriy: bitta ticker) chatga chiqaradi."""
    try:
        day = datetime.strptime(REPLAY_DAY, "%Y-%m-%d").date()
    except ValueError:
        sys.exit("XATO: kun YYYY-MM-DD ko'rinishida bo'lsin, masalan 2026-11-20")
    _, trades = fetch_trades(REPLAY_DAY)
    if trades is None:
        sys.exit("XATO: OptionWhales javob bermadi (kun yoki API kalitni tekshiring)")
    picked, ids = [], set()
    for raw in trades:
        x = parse(raw)
        if not x or x["id"] in ids or not wanted(x, day):
            continue
        if REPLAY_TICKER and x["ticker"] != REPLAY_TICKER:
            continue
        ids.add(x["id"])
        picked.append(x)
    picked.sort(key=lambda x: x["ts"])
    log(f"{REPLAY_DAY} {REPLAY_TICKER or 'barcha tickerlar'}: lentada {len(trades)} ta bitim, mos {len(picked)} ta")
    if len(picked) > REPLAY_MAX:
        log(f"Faqat eng yirik {REPLAY_MAX} tasi yuboriladi")
        picked = sorted(sorted(picked, key=lambda x: -x["premium"])[:REPLAY_MAX], key=lambda x: x["ts"])
    sent = 0
    for x in picked:
        if send(x):
            sent += 1
            time.sleep(3.2)
    log(f"Tugadi: {sent} ta xabar yuborildi")


def main():
    missing = [n for n, v in (("OW_API_KEY", OW_KEY), ("FLOW_BOT_TOKEN", BOT_TOKEN), ("FLOW_CHAT_ID", CHAT_ID)) if not v]
    if missing and not (TEST and "OW_API_KEY" in missing and len(missing) == 1):
        sys.exit("XATO: secret topilmadi: " + ", ".join(missing))
    if TEST:
        ok = send(sample())
        sys.exit(0 if ok else "XATO: sinov xabari yuborilmadi (token, chat ID yoki bot huquqlarini tekshiring)")

    if REPLAY_DAY:
        replay()
        return

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
