"""Multi-day synthetic Binance + Bybit BTCUSDT data in the partitioned layout read by src/new_backtest.

Every day has its OWN random stream (seed*1000 + day index), so changing the parameters of later days cannot
change earlier days. That property is what the look-ahead invariance test relies on.
"""
import gzip, json, math, random
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = 67_000.0
PAIR = "BTCUSDT"
REGIME = {  # per-day: trade probability per tick, volatility (price sd per tick), wide-venue half-spread (bps)
    "p_trade": [0.35, 0.45, 0.30, 0.50, 0.40, 0.35, 0.45, 0.30, 0.50, 0.40, 0.35, 0.45],
    "vol":     [0.5, 0.7, 0.6, 0.9, 0.5, 0.8, 0.6, 1.0, 0.7, 0.5, 0.9, 0.6],
    "wide":    [12, 10, 14, 12, 11, 13, 12, 10, 14, 12, 11, 13],
}
LAT = {"binance": (0.004, 0.002), "bybit": (0.009, 0.003)}     # base + exponential jitter, seconds


def day_params(d, overrides):
    p = {k: v[d % len(v)] for k, v in REGIME.items()}
    p.update((overrides or {}).get(d, {}))
    return p


def gen(root, ndays=12, start="2026-10-01", seed=11, session_s=150.0, step=0.05, levels=8, seg_s=15.0,
        overrides=None, linked_depth=False, clock_offset=None, snapshot_at=75.0, hour=12, minute=0, bursty=False):
    """Returns the list of dates written. clock_offset: {venue: seconds added to local_ts} (collector clock error)."""
    root = Path(root); n = int(session_s / step)
    d0 = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
    dates = []
    for d in range(ndays):
        day = (d0 + timedelta(days=d)); date = day.strftime("%Y-%m-%d"); dates.append(date)
        t0 = day.replace(hour=hour, minute=minute).timestamp()
        P = day_params(d, overrides); rng = random.Random(seed * 1000 + d)
        busy = [random.Random(seed * 7919 + d * 131 + i).random() < 0.5 for i in range(int(session_s // seg_s) + 2)]   # separate stream: toggling bursty never moves the main one
        mid = BASE + 40.0 * d + rng.gauss(0, 20)
        out = {v: {"snap": [], "ev": [], "tr": []} for v in ("binance", "bybit")}
        books = {v: {"bid": {}, "ask": {}} for v in out}; last_local = {v: 0.0 for v in out}
        for k in range(n + 1):
            tr = k * step; t = t0 + tr
            mid += rng.gauss(0, P["vol"])
            wide_venue = "bybit" if int(tr // seg_s) % 2 == 0 else "binance"      # alternate so inventory oscillates
            for v in ("binance", "bybit"):
                vmid = mid + rng.gauss(0, 0.15)
                bb = math.floor(vmid * 10) / 10
                w = round(vmid * P["wide"] / 1e4, 1) if v == wide_venue else 0.0
                best_bid = round(bb - w, 1); best_ask = round(bb + 0.1 + w, 1)
                tgt_b = {round(best_bid - 0.1 * i, 1) for i in range(levels)}
                tgt_a = {round(best_ask + 0.1 * i, 1) for i in range(levels)}
                lat = LAT[v][0] + rng.expovariate(1 / LAT[v][1])
                local = max(t + lat + (clock_offset or {}).get(v, 0.0), last_local[v]); last_local[v] = local
                bk = books[v]
                new_b = {p: (bk["bid"][p] if p in bk["bid"] and rng.random() > 0.25 else round(rng.uniform(0.4, 4.0), 3)) for p in tgt_b}
                new_a = {p: (bk["ask"][p] if p in bk["ask"] and rng.random() > 0.25 else round(rng.uniform(0.4, 4.0), 3)) for p in tgt_a}
                if k == 0:
                    bk["bid"], bk["ask"] = new_b, new_a
                    out[v]["snap"].append({"timestamp": t, "local_ts": local, "best_bid": max(new_b), "best_ask": min(new_a),
                                           "bids": [[p, q] for p, q in sorted(new_b.items(), reverse=True)],
                                           "asks": [[p, q] for p, q in sorted(new_a.items())]})
                    continue
                for side, new, old in (("bid", new_b, bk["bid"]), ("ask", new_a, bk["ask"])):
                    for p in sorted(set(new) | set(old)):
                        nq, oq = new.get(p, 0.0), old.get(p, 0.0)
                        if nq != oq:
                            out[v]["ev"].append({"exchange_ts": t, "local_ts": local, "side": side, "price": p, "volume": nq,
                                                 "event_type": "delete" if nq == 0 else "modify"})
                bk["bid"], bk["ask"] = new_b, new_a
                if snapshot_at and abs(tr - snapshot_at) < step / 2:                 # periodic full snapshot, as real feeds send
                    out[v]["snap"].append({"timestamp": t + 0.0005, "local_ts": local + 0.0005, "best_bid": max(new_b), "best_ask": min(new_a),
                                           "bids": [[p, q] for p, q in sorted(new_b.items(), reverse=True)],
                                           "asks": [[p, q] for p, q in sorted(new_a.items())]})
                p_tr = min(0.95, P["p_trade"] * (2.2 if busy[int(tr // seg_s)] else 0.15)) if bursty else P["p_trade"]
                if rng.random() < p_tr:                                                # a public trade at the touch
                    buy = rng.random() < 0.5
                    px = min(new_a) if buy else max(new_b)
                    vol = round(min(0.8, max(0.001, rng.lognormvariate(math.log(0.04), 0.9))), 3)
                    tt = t + 0.001
                    out[v]["tr"].append({"exchange_ts": tt, "local_ts": max(local + 0.001, last_local[v]), "price": px, "volume": vol,
                                         "side": "b" if buy else "s"})
                    if linked_depth:                                                  # the same execution also shows up as a depth reduction
                        side = "ask" if buy else "bid"; book = bk[side]
                        if px in book:
                            newq = round(max(0.0, book[px] - vol), 3)
                            book[px] = newq
                            out[v]["ev"].append({"exchange_ts": tt + 0.0005, "local_ts": local + 0.0015, "side": side, "price": px, "volume": newq,
                                                 "event_type": "delete" if newq == 0 else "modify"})
        for v, dd in out.items():
            for kind, rows in (("book_snapshots", dd["snap"]), ("book_events", dd["ev"]), ("trades", dd["tr"])):
                p = root / v / kind / f"pair={PAIR}" / f"date={date}" / "part-000.jsonl.gz"
                p.parent.mkdir(parents=True, exist_ok=True)
                with gzip.open(p, "wt", encoding="utf-8") as fh:
                    for r in rows: fh.write(json.dumps(r) + "\n")
    return dates
