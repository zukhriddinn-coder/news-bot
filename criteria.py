"""
Trading Laboratory -> Buffet AI candidate criteria (one place, used by scanner.py and buffetai.py)

The 26.10 rules (the ones that found NTNX, NTAP, CME, ERIC, UNP), unchanged:
  1. Buffet AI says LONG on the daily chart, TP1 pays at least 1:1, RSI below 80, stop at most 12% away
  2. Time Machine: TP1's share of the hits beats break-even 1/(1+R) by 5+ points
  3. Risk check: nothing blocking
  4. The same rule on this stock's last 250 bars: removed if it fired 5+ times and lost on average
  5. No earnings inside the plan (~4 weeks)
Added on 27.10:
  6. 5-year test: this stock's own record with the same rule over 5 years (independent trades, after costs,
     from the weekly calibration). Removed if it has 15+ trades and lost on average.
  7. Weekly calibration (whole market, 5 years, two periods): removed if the signal belongs to a group of
     signals that lost money in BOTH 2021-24 and 2025-26. A group that made money in both is marked.
"""

import time

import numpy as np

import buffetai as B

MIN_RR = 1.0
MAX_RSI = 80
MAX_RISK_PCT = 12
MIN_EDGE = 0.05                 # Time Machine: 5+ points above break-even
HIST_BARS = 250                 # the 26.10 history test: last 250 bars
HIST_MIN_FIRED = 5
FIVE_Y_MIN_TRADES = 15          # fewer trades in 5 years = not enough to judge
CAL_MIN_DISC, CAL_MIN_VAL = 200, 150             # trades a market-wide group needs in each period
CAL_BAD = (-0.05, -0.03)        # avg R in 2021-24 and 2025-26 at or below these = a losing group
CAL_GOOD = (0.05, 0.03)         # ... at or above these = a winning group


# ----------------------------- 1-3: the plan and Time Machine ---------------

def plan_gates(a):
    """[(ok, text)] for the plan itself (rule 1)."""
    p = a["plan"]
    t = (p.get("targets") or [{}])[0]
    return [
        (p["side"] == "LONG", f"Buffet AI LONG deydi (ball {a['score']:+d})" if p["side"] == "LONG"
         else f"Buffet AI LONG demayapti (hozir {p['side']})"),
        (bool(t) and t.get("rr", 0) >= MIN_RR, f"TP1 foydasi riskdan kam emas (1:{t.get('rr', 0)})"),
        (a["rsi"] < MAX_RSI, f"RSI {MAX_RSI} dan past ({a['rsi']})"),
        (p.get("risk_pct", 99) <= MAX_RISK_PCT, f"Stop {MAX_RISK_PCT}% ichida ({p.get('risk_pct', 0)}%)"),
    ]


def time_machine(df, a):
    """Rule 2: the 26.10 Time Machine check of the plan."""
    p = a["plan"]
    mc = B.simulate(B.bar_pool(df), "LONG", p["entry"], p["stop"], p["targets"][0]["price"], B.HORIZON["1D"])
    kind, verdict = B.mc_verdict(mc["p_target"], mc["p_stop"], mc["r_target"])
    hit = mc["p_target"] + mc["p_stop"]
    edge = (mc["p_target"] / hit - 1 / (1 + mc["r_target"])) if hit else -1
    mc.update(verdict_kind=kind, verdict=verdict, edge=round(edge, 3), share=(mc["p_target"] / hit if hit else 0),
              even=1 / (1 + mc["r_target"]))
    return mc


def tm_ok(mc):
    return mc["edge"] >= MIN_EDGE


# ----------------------------- 4: the 250-bar history (26.10, unchanged) ----

def history_250(df, side="LONG", bars=None, max_tests=HIST_BARS, deadline=None):
    """The same Buffet AI rule on past bars (the 26.10 version): every past bar where it said the same side,
    its own plan (entry, stop, TP1) played forward on the real bars that followed."""
    bars = bars or B.HORIZON["1D"]
    h_all, l_all, c_all = (df[k].to_numpy(dtype=float) for k in ("High", "Low", "Close"))
    n = len(df)
    start = max(60, n - bars - max_tests)
    fired = win = loss = 0
    Rs = []
    t = start
    while t < n - bars:
        if deadline and time.time() > deadline:
            break
        a = B.analyse(df.iloc[:t + 1], light=True)
        p = a["plan"]
        if p["side"] != side or not p.get("targets"):
            t += 1
            continue
        fired += 1
        e, st, tg = p["entry"], p["stop"], p["targets"][0]["price"]
        risk = abs(e - st)
        out = None
        for j in range(t + 1, t + 1 + bars):
            if side == "LONG":
                if l_all[j] <= st:
                    out = -1.0
                    break
                if h_all[j] >= tg:
                    out = (tg - e) / risk
                    break
            else:
                if h_all[j] >= st:
                    out = -1.0
                    break
                if l_all[j] <= tg:
                    out = (e - tg) / risk
                    break
        if out is None:
            last = c_all[t + bars]
            out = ((last - e) if side == "LONG" else (e - last)) / risk
        else:
            win += out > 0
            loss += out < 0
        Rs.append(out)
        t += max(1, bars // 4)            # do not count one long signal as many trades
    return {"fired": fired, "target": int(win), "stop": int(loss), "neither": fired - int(win) - int(loss),
            "avg_R": round(float(np.mean(Rs)), 2) if Rs else None, "tested_bars": n - bars - start}


def history_ok(h):
    return not (h and h["fired"] >= HIST_MIN_FIRED and (h["avg_R"] or 0) < 0)


# ----------------------------- 6: 5-year test of this stock -----------------

def five_year(sym, cal, df=None, deadline=None):
    """This stock's own 5-year record with the same rule and the same exit (Buffet AI's plan).
    From the weekly calibration (independent trades, after costs); if the stock is not in it and 5 years of
    bars are given, it is computed here."""
    st = (((cal or {}).get("per_stock") or {}).get(sym) or {}).get("A")
    if st:
        return dict(st, source="calibration", enough=st["trades"] >= FIVE_Y_MIN_TRADES)
    if df is not None and len(df) >= 1000:
        h = B.history_test(df, "LONG", B.HORIZON["1D"], deadline=deadline)
        return {"trades": h["trades"], "target": h["target"], "stop": h["stop"], "neither": h["neither"],
                "avg_R": h["avg_R"], "win_pct": h["win_pct"], "source": "here", "enough": h["enough"]}
    return None


def five_year_ok(f5):
    return not (f5 and f5["enough"] and (f5["avg_R"] or 0) < 0)


# ----------------------------- 7: weekly calibration ------------------------

def calibration_check(a, df, cal, spy_ctx):
    """Which market-wide groups (5 years, Buffet AI's own exit) this LONG signal belongs to.
    Returns {"ok", "bad": worst losing group or None, "good": best winning group or None} or None."""
    if not cal or cal.get("version", 1) < 2:
        return None
    import rules as R
    f = R.features(a, df.iloc[-501:], spy_ctx)
    groups = cal.get("groups", {})
    bad, good = [], []
    for key in R.keys_of(f):
        if key == "all":
            continue
        st = (groups.get(key) or {}).get("A")
        if not st:
            continue
        d, v = st["disc"], st["val"]
        if d["trades"] < CAL_MIN_DISC or v["trades"] < CAL_MIN_VAL:
            continue
        item = {"key": key, "label": R.human(key), "disc": d, "val": v}
        if d["avg_R"] <= CAL_BAD[0] and v["avg_R"] <= CAL_BAD[1]:
            bad.append(item)
        elif d["avg_R"] >= CAL_GOOD[0] and v["avg_R"] >= CAL_GOOD[1]:
            good.append(item)
    worst = min(bad, key=lambda x: x["disc"]["avg_R"] + x["val"]["avg_R"]) if bad else None
    best = max(good, key=lambda x: x["disc"]["avg_R"] + x["val"]["avg_R"]) if good else None
    return {"ok": not bad, "bad": worst, "good": best, "regime": f["bozor"],
            "updated": (cal.get("updated") or "")[:10]}
