"""
Trading Laboratory -> Buffet AI weekly calibration
Once a week (GitHub Actions, Saturday) this script re-runs the FULL daily-candidate rule of scanner.py
(Buffet AI LONG plan + quality gates + Time Machine) bar by bar on 5 years of daily bars of every stock in the
scanner's universe, and plays every signal's own plan (entry, stop, TP1) forward on the real bars that followed.
Trades never overlap. The result, calibration.json, holds:
  - market-wide statistics by signal type (all, score, trend, setup, year): thousands of real trades;
  - every stock's own record (its independent trades over 5 years).
scanner.py and buffetai.py read this file, so the daily scans stay fast.
"""

import json
import os
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone

import numpy as np

import buffetai as B
import scanner as S

OUT_FILE = "calibration.json"
YEARS = "6y"                  # 5 years of signals + 1 year of warm-up for EMA 200 / levels
HORIZON = B.HORIZON["1D"]     # 20 trading days, the same as the scanner
MIN_GROUP = 100               # a group needs at least this many trades to be shown


def candidate_gate(a, past):
    """The scanner's own gates on a past bar (only the past is used)."""
    p = a["plan"]
    t0 = p["targets"][0]
    if t0["rr"] < 1.0 or a["rsi"] >= S.MAX_RSI or p["risk_pct"] > S.MAX_RISK_PCT:
        return False
    pool = B.bar_pool(past)
    if len(pool) < 60:
        return False
    mc = B.simulate(pool, "LONG", p["entry"], p["stop"], t0["price"], HORIZON, replays=2000)
    hit = mc["p_target"] + mc["p_stop"]
    edge = (mc["p_target"] / hit - 1 / (1 + mc["r_target"])) if hit else -1
    return edge >= S.MIN_EDGE and mc["p_target"] > mc["p_stop"]


def one_stock(args):
    sym, df = args
    try:
        trades = B.replay_signals(df, "LONG", HORIZON, start=260, gate=candidate_gate)
    except Exception as exc:
        print(f"{sym}: {exc}")
        trades = []
    for t in trades:
        t["symbol"] = sym
    return sym, trades


def score_bucket(s):
    return "ball +2" if s <= 2 else "ball +3" if s == 3 else "ball +4 va yuqori"


def groups_of(t):
    g = ["all", score_bucket(t["score"]), f"trend: {t['trend']}", f"yil: {t['date'][:4]}",
         f"R/R: {'1–1.5' if t['rr'] < 1.5 else '1.5–2.5' if t['rr'] < 2.5 else '2.5+'}"]
    g += [f"setap: {s}" for s in t["setups"]] or ["setap: yo'q"]
    return g


def main():
    started = time.time()
    stocks = S.universe()
    prices = S.download(stocks, period=YEARS, min_bars=600)
    liquid = {}
    for s, df in prices.items():
        c, v = df["Close"].to_numpy(dtype=float), df["Volume"].to_numpy(dtype=float)
        if c[-1] >= S.MIN_PRICE and np.mean(v[-21:-1] * c[-21:-1]) >= S.MIN_DOLLAR_VOLUME:
            liquid[s] = df
    print(f"Universe {len(stocks)}, with 6y prices {len(prices)}, liquid {len(liquid)}")

    all_trades, per_stock = [], {}
    workers = max(1, os.cpu_count() or 1)
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for k, (sym, trades) in enumerate(pool.map(one_stock, liquid.items(), chunksize=4), 1):
            all_trades += trades
            per_stock[sym] = B.summarize(trades)
            if k % 100 == 0:
                print(f"  {k}/{len(liquid)} stocks, {len(all_trades)} trades, {time.time() - started:.0f}s")

    groups = defaultdict(list)
    for t in all_trades:
        for g in groups_of(t):
            groups[g].append(t)
    stats = {g: B.summarize(ts) for g, ts in groups.items() if len(ts) >= MIN_GROUP or g == "all"}
    out = {"updated": datetime.now(timezone.utc).isoformat(timespec="minutes"), "years": 5,
           "horizon_bars": HORIZON, "stocks": len(liquid), "groups": stats,
           "per_stock": {s: v for s, v in per_stock.items() if v["trades"]}}
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    a = stats.get("all", {})
    print(f"Saved: {len(all_trades)} trades from {len(liquid)} stocks in {time.time() - started:.0f}s. "
          f"All: TP1 {a.get('win_pct')}%, avg {a.get('avg_R')}R")
    for g in sorted(stats):
        v = stats[g]
        print(f"  {g:45} {v['trades']:6} trades  TP1 {v['win_pct']:5}%  stop {v['stop'] / v['trades'] * 100:5.1f}%  "
              f"avg {v['avg_R']:+.3f}R")


if __name__ == "__main__":
    main()
