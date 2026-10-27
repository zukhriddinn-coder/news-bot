"""
Trading Laboratory -> Buffet AI weekly calibration (v2)
Once a week (GitHub Actions, Saturday) this script re-runs Buffet AI's LONG rule bar by bar on 6 years of
daily bars of every stock in the scanner's universe. For every signal it records its features (rules.py:
market regime, relative strength, volume, RSI, Time Machine, setup, score, trend) and plays four exit
variants forward on the real bars that followed, after costs. Trades never overlap.

To avoid fooling ourselves, rules are FOUND on 2021-2024 ("disc") and CHECKED on 2025-2026 ("val").
Only a rule that is positive in both periods is "approved"; the daily scanner gives candidates only for
approved rules. Output: calibration.json.
"""

import json
import os
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone

import numpy as np

import buffetai as B
import rules as R
import scanner as S

OUT_FILE = "calibration.json"
YEARS = "6y"
SPLIT = "2025-01-01"          # before: find rules ("disc"); from this date: check them ("val")
MIN_DISC, MIN_VAL = 200, 60   # trades a rule needs in each period
MIN_DISC_R, MIN_VAL_R = 0.05, 0.03   # average R after costs a rule needs in each period
_SPY = {}


def one_stock(args):
    sym, df = args
    o, h, l, c = (df[k].to_numpy(dtype=float) for k in ("Open", "High", "Low", "Close"))
    n = len(df)
    trades = []
    t = 260
    try:
        while t < n - 2:
            a = B.analyse(df.iloc[max(0, t + 1 - B.ANALYSE_WINDOW):t + 1], light=True)
            if not R.basic_gate(a):
                t += 1
                continue
            f = R.features(a, df.iloc[max(0, t - 500):t + 1], _SPY)
            res, end = {}, t + 1
            for v in R.VARIANTS:
                e, st, tp = R.variant_plan(v, a)
                out = R.play(o, h, l, c, t, e, st, tp)
                if out is None:
                    break
                res[v] = (round(float(out[0]), 3), out[1])
                end = max(end, out[2])
            if len(res) < len(R.VARIANTS):
                break                                        # the data ends before the trade could finish
            trades.append({"date": str(df.index[t].date()), "f": f, "r": res})
            t = end + 1                                      # next trade only after this one has ended
    except Exception as exc:
        print(f"{sym}: {exc}")
    return sym, trades


def new_stat():
    return {"n": 0, "target": 0, "stop": 0, "sum": 0.0}


def add(st, r):
    st["n"] += 1
    st["sum"] += r[0]
    st["target"] += r[1] == "target"
    st["stop"] += r[1] == "stop"


def final(st):
    n = st["n"]
    return {"trades": n, "target": st["target"], "stop": st["stop"], "neither": n - st["target"] - st["stop"],
            "avg_R": round(st["sum"] / n, 3) if n else None, "win_pct": round(st["target"] / n * 100, 1) if n else None}


def main():
    started = time.time()
    stocks = S.universe()
    prices = S.download(list(stocks) + ["SPY"], period=YEARS, min_bars=600)
    spy = prices.pop("SPY", None)
    if spy is None:
        raise SystemExit("SPY could not be downloaded")
    _SPY.update(R.spy_context(spy))
    liquid = {}
    for s, df in prices.items():
        if s not in stocks:
            continue
        c, v = df["Close"].to_numpy(dtype=float), df["Volume"].to_numpy(dtype=float)
        if c[-1] >= S.MIN_PRICE and np.mean(v[-21:-1] * c[-21:-1]) >= S.MIN_DOLLAR_VOLUME:
            liquid[s] = df
    print(f"Universe {len(stocks)}, with prices {len(prices)}, liquid {len(liquid)}")

    stats = defaultdict(lambda: defaultdict(lambda: {"disc": new_stat(), "val": new_stat(), "all": new_stat()}))
    per_stock, total = {}, 0
    with ProcessPoolExecutor(max_workers=max(1, os.cpu_count() or 1)) as pool:
        for k, (sym, trades) in enumerate(pool.map(one_stock, liquid.items(), chunksize=4), 1):
            ps = {v: new_stat() for v in R.VARIANTS}
            for tr in trades:
                period = "disc" if tr["date"] < SPLIT else "val"
                keys = R.keys_of(tr["f"]) + [f"yil: {tr['date'][:4]}"]
                for v, r in tr["r"].items():
                    add(ps[v], r)
                    for key in keys:
                        add(stats[key][v][period], r)
                        add(stats[key][v]["all"], r)
            per_stock[sym] = {v: final(st) for v, st in ps.items() if st["n"]}
            total += len(trades)
            if k % 100 == 0:
                print(f"  {k}/{len(liquid)} stocks, {total} trades, {time.time() - started:.0f}s")

    groups = {key: {v: {p: final(st) for p, st in per.items()} for v, per in vs.items()}
              for key, vs in stats.items() if vs["A"]["all"]["n"] >= 100}

    approved = []
    for key, vs in groups.items():
        best = None
        for v, per in vs.items():                         # choose the exit on the DISCOVERY period only
            d = per["disc"]
            if d["trades"] >= MIN_DISC and d["avg_R"] >= MIN_DISC_R and (best is None or d["avg_R"] > best[1]["avg_R"]):
                best = (v, d)
        if not best:
            continue
        val = vs[best[0]]["val"]
        if val["trades"] >= MIN_VAL and val["avg_R"] >= MIN_VAL_R:   # ... then it must also work later
            approved.append({"rule": key, "variant": best[0], "disc": best[1], "val": val,
                             "all": vs[best[0]]["all"]})
    approved.sort(key=lambda x: (x["val"]["avg_R"] + x["disc"]["avg_R"]) / 2, reverse=True)

    today = R.spy_context(spy)[spy.index[-1].date()]
    out = {"version": 2, "updated": datetime.now(timezone.utc).isoformat(timespec="minutes"), "years": 5,
           "split": SPLIT, "horizon_bars": R.HORIZON, "stocks": len(liquid), "trades": total,
           "variants": R.VARIANTS, "groups": groups, "approved": approved,
           "per_stock": {s: v for s, v in per_stock.items() if v},
           "spy_today": {"above_ma200": today[0]}}
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))

    print(f"\nSaved {total} trades from {len(liquid)} stocks in {time.time() - started:.0f}s")
    print(f"\n{'group':62} var  disc: n / TP1 / avgR      val: n / TP1 / avgR")
    order = sorted(groups, key=lambda k: -(groups[k]["A"]["all"]["trades"]))
    for key in order[:60]:
        for v in R.VARIANTS:
            d, va = groups[key][v]["disc"], groups[key][v]["val"]
            if d["trades"] and va["trades"]:
                print(f"{key[:62]:62} {v}   {d['trades']:6} {d['win_pct']:5}% {d['avg_R']:+.3f}   "
                      f"{va['trades']:6} {va['win_pct']:5}% {va['avg_R']:+.3f}")
    print(f"\nAPPROVED RULES: {len(approved)}")
    for r in approved[:40]:
        print(f"  {r['rule'][:70]:70} {r['variant']}  disc {r['disc']['trades']} {r['disc']['avg_R']:+.3f}R  "
              f"val {r['val']['trades']} {r['val']['avg_R']:+.3f}R")


if __name__ == "__main__":
    main()
