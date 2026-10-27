"""
Trading Laboratory -> Buffet AI rule features (shared by calibrate.py and scanner.py)
One place that defines, for a LONG signal on a daily bar:
  - its features (market regime, relative strength, volume, RSI, Time Machine, setup, score, trend);
  - the exit variants that are tested (stop / take profit);
  - how a rule (a set of feature values) matches a signal.
The weekly calibration finds which rules really worked over 5 years; the daily scanner only uses those.
"""

import numpy as np

import buffetai as B

HORIZON = B.HORIZON["1D"]          # 20 trading days
COST_PCT = 0.001                    # 0.05% per side: commission + slippage, charged on every trade
MIN_VAL_TRADES = 150                # an approved rule needs 150+ trades in the 2025-26 check period

# Exit variants. "A" = Buffet AI's own plan (stop under the swing, TP1 at the nearest level).
VARIANTS = {
    "A": "Buffet AI rejasi (stop swing ostida, TP1 eng yaqin daraja)",
    "B": "stop 2 ATR, TP 2 ATR (1R)",
    "C": "stop 2 ATR, TP 4 ATR (2R)",
    "D": "stop 1.5 ATR, TP 3 ATR (2R)",
}


def spy_context(spy_df):
    """{date: (above_ma200, spy_63d_return)} from SPY daily bars."""
    c = spy_df["Close"].to_numpy(dtype=float)
    ma = np.convolve(c, np.ones(200) / 200, mode="full")[:len(c)]
    out = {}
    for i, d in enumerate(spy_df.index):
        above = bool(c[i] > ma[i]) if i >= 199 else None
        r63 = float(c[i] / c[i - 63] - 1) if i >= 63 else None
        out[d.date()] = (above, r63)
    return out


def tm_ok(a, past):
    """Time Machine verdict for the plan (True = TP1 side beats break-even by 5+ points and comes first more)."""
    p = a["plan"]
    t0 = p["targets"][0]
    pool = B.bar_pool(past)
    if len(pool) < 60:
        return False
    mc = B.simulate(pool, "LONG", p["entry"], p["stop"], t0["price"], HORIZON, replays=2000)
    hit = mc["p_target"] + mc["p_stop"]
    edge = (mc["p_target"] / hit - 1 / (1 + mc["r_target"])) if hit else -1
    return edge >= 0.05 and mc["p_target"] > mc["p_stop"]


def features(a, past, spy_ctx):
    """Feature values of a LONG signal on the last bar of `past` (only the past is used)."""
    c = past["Close"].to_numpy(dtype=float)
    d = past.index[-1].date()
    above, spy63 = spy_ctx.get(d, (None, None))
    r63 = float(c[-1] / c[-64] - 1) if len(c) > 64 else None
    rs = (r63 - spy63) if (r63 is not None and spy63 is not None) else None
    rsi = a["rsi"]
    return {
        "bozor": None if above is None else ("SPY>MA200" if above else "SPY<MA200"),
        "kuch": None if rs is None else ("RS<0" if rs < 0 else "RS 0-10%" if rs < 0.10 else "RS>10%"),
        "hajm": "hajm 1.5x+" if a.get("volume_confirmed") else "hajm oddiy",
        "rsi": "RSI<50" if rsi < 50 else "RSI 50-60" if rsi < 60 else "RSI 60-70" if rsi < 70 else "RSI 70-80",
        "tm": "TM ustun" if tm_ok(a, past) else "TM yo'q",
        "setap": [s["name"] for s in a["setups"]] or ["yo'q"],
        "ball": "ball +2" if a["score"] <= 2 else "ball +3" if a["score"] == 3 else "ball +4+",
        "trend": a["trend"],
    }


def basic_gate(a):
    """Quality gates that are always on (before any rule)."""
    p = a["plan"]
    return (p["side"] == "LONG" and p.get("targets") and p["targets"][0]["rr"] >= 1.0 and a["rsi"] < 80
            and p["risk_pct"] <= 12)


def variant_plan(v, a):
    """(entry, stop, take profit) of an exit variant."""
    p = a["plan"]
    e, atr = p["entry"], a["atr"]
    if v == "A":
        return e, p["stop"], p["targets"][0]["price"]
    k_stop, k_tp = {"B": (2, 2), "C": (2, 4), "D": (1.5, 3)}[v]
    return e, e - k_stop * atr, e + k_tp * atr


def play(o, h, l, c, t, e, st, tp, bars=HORIZON):
    """Play a LONG plan forward from bar t+1 on real bars. Returns (R after costs, result, exit index) or
    None when the data ends before the trade could finish."""
    risk = e - st
    if risk <= 0:
        return None
    cost = COST_PCT * 2 * e / risk
    n = len(c)
    for j in range(t + 1, min(t + bars, n - 1) + 1):
        if o[j] >= tp:                                   # gap up through the target: filled at the open
            return (o[j] - e) / risk - cost, "target", j
        if o[j] <= st:                                   # gap down through the stop: filled at the open
            return (o[j] - e) / risk - cost, "stop", j
        if l[j] <= st:                                   # stop first if both are hit inside one bar
            return (st - e) / risk - cost, "stop", j
        if h[j] >= tp:
            return (tp - e) / risk - cost, "target", j
    if t + bars > n - 1:
        return None
    j = t + bars
    return (c[j] - e) / risk - cost, "neither", j


def keys_of(f):
    """Group keys of a feature set: single features, and the market regime combined with the others."""
    singles = [f"{k}: {f[k]}" for k in ("bozor", "kuch", "hajm", "rsi", "tm", "ball", "trend") if f[k]]
    singles += [f"setap: {s}" for s in f["setap"]]
    keys = ["all"] + singles
    if f["bozor"]:
        reg = f"bozor: {f['bozor']}"
        keys += [f"{reg} + {s}" for s in singles if not s.startswith("bozor")]
        if f["kuch"]:
            keys += [f"{reg} + kuch: {f['kuch']} + {x}" for x in
                     [f"setap: {s}" for s in f["setap"]] + [f"tm: {f['tm']}", f"hajm: {f['hajm']}", f"rsi: {f['rsi']}"]]
    return keys


def rule_matches(rule_key, f):
    return rule_key in keys_of(f)


LABELS = {
    "bozor: SPY>MA200": "bozor o'sish rejimida (SPY > 200 kunlik o'rtacha)",
    "bozor: SPY<MA200": "bozor pasayish rejimida (SPY < 200 kunlik o'rtacha)",
    "kuch: RS<0": "aksiya 3 oyda SPY'dan zaifroq",
    "kuch: RS 0-10%": "aksiya 3 oyda SPY'dan 0–10% kuchliroq",
    "kuch: RS>10%": "aksiya 3 oyda SPY'dan 10%+ kuchliroq",
    "hajm: hajm 1.5x+": "hajm o'rtachadan 1.5x+",
    "hajm: hajm oddiy": "hajm odatiy",
    "tm: TM ustun": "Time Machine TP1 tomonda",
    "tm: TM yo'q": "Time Machine ustunlik ko'rsatmagan",
    "all": "barcha Buffet AI LONG signallari",
}


def human(rule_key):
    """'bozor: SPY>MA200 + kuch: RS>10%' -> readable Uzbek."""
    parts = []
    for p in rule_key.split(" + "):
        if p in LABELS:
            parts.append(LABELS[p])
        elif p.startswith("setap: "):
            parts.append("setap — " + p[7:])
        elif p.startswith("trend: "):
            parts.append("trend: " + p[7:])
        elif p.startswith(("rsi: ", "ball: ")):
            parts.append(p.split(": ", 1)[1].replace("RSI", "RSI "). replace("  ", " "))
        else:
            parts.append(p)
    return " + ".join(parts)
