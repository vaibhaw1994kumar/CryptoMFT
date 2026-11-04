"""Tick check (owner, 2026-11-05): the smallest price step seen in each venue's book, Jan 2022 and Mar 2024, against the declared ticks. Per venue-day: (a) the smallest positive gap between adjacent levels of a snapshot,
(b) the gcd of every price seen (snapshot levels, and book_events prices), (c) the share of prices on the declared grid. Samples: first N snapshots / events (stated)."""
import glob, gzip, json, math
D = "/Users/vaibhav/Documents/Newproject/data"
DECL = {("bybit", "2022"): 0.005, ("binance", "2022"): 0.001, ("bybit", "2024"): 0.001, ("binance", "2024"): 0.001}
DAYS = ("2022-01-05", "2022-01-13", "2024-03-05", "2024-03-12")
def rows(fp, n):
    c = 0
    try:
        with gzip.open(fp, "rt") as fh:
            for line in fh:
                try: yield json.loads(line)
                except Exception: return
                c += 1
                if c >= n: return
    except (EOFError, OSError): return
res = []
for v in ("bybit", "binance"):
    for d in DAYS:
        decl = DECL[(v, d[:4])]
        mingap, g, tot, on, ns = None, 0, 0, 0, 0
        for fp in sorted(glob.glob(f"{D}/{v}/book_snapshots/pair=SOLUSDT/date={d}/*.gz"))[:1]:
            for r in rows(fp, 20000):
                ns += 1
                for side in ("bids", "asks"):
                    ps = sorted({round(float(l[0]), 8) for l in r.get(side, [])})
                    for a, b in zip(ps, ps[1:]):
                        if b > a and (mingap is None or b - a < mingap): mingap = b - a
                    for p in ps:
                        g = math.gcd(g, round(p * 1e6)); tot += 1; on += abs(p / decl - round(p / decl)) < 1e-6
        ne, ge, tote, one = 0, 0, 0, 0
        for fp in sorted(glob.glob(f"{D}/{v}/book_events/pair=SOLUSDT/date={d}/*.gz"))[:1]:
            for r in rows(fp, 300000):
                p = float(r.get("price") or 0)
                if p > 0:
                    ne += 1; ge = math.gcd(ge, round(p * 1e6)); one += abs(p / decl - round(p / decl)) < 1e-6
        res.append({"venue": v, "day": d, "declared_tick": decl, "snapshots_read": ns, "min_adjacent_gap": mingap, "gcd_snapshot_prices": g / 1e6 if g else None,
                    "share_snapshot_prices_on_declared_grid": on / tot if tot else None, "book_events_read": ne, "gcd_event_prices": ge / 1e6 if ge else None,
                    "share_event_prices_on_declared_grid": one / ne if ne else None})
        print(res[-1], flush=True)
json.dump(res, open("tick_check_step9.json", "w"), indent=1)
