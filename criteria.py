"""
Trading Laboratory -> Buffet AI candidate criteria (one place, used by scanner.py and buffetai.py)

The 26.10 rules (the ones that found NTNX, NTAP, CME, ERIC, UNP), unchanged:
  1. Buffet AI says LONG on the daily chart, TP1 pays at least 1:1, RSI below 80, stop at most 12% away
  2. Time Machine: TP1's share of the hits beats break-even 1/(1+R) by 5+ points
  3. Risk check: nothing blocking
  4. The same rule on this stock's last 250 bars: removed if it fired 5+ times and lost on average
  5. No earnings inside the plan (~4 weeks)
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
