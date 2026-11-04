"""2-hour slices (00:00-02:00 UTC) of the March-2024 SOLUSDT files, same layout as data/, written under out/queue_v2/slices_mar2024/. A slice ends where the file reading stops (exchange time >= start+7200).
usage: make_slices.py DAY [DAY ...]"""
import calendar, glob, gzip, json, os, sys, time
SRC = "/Users/vaibhav/Documents/Newproject/data"
OUT = "/Users/vaibhav/Documents/Newproject/out/queue_v2/slices_mar2024"
KEYS = {"book_snapshots": "timestamp", "book_events": "exchange_ts", "trades": "exchange_ts"}
for day in sys.argv[1:]:
    t0 = calendar.timegm(time.strptime(day, "%Y-%m-%d")); t1 = t0 + 7200
    for venue in ("bybit", "binance"):
        for st, key in KEYS.items():
            files = sorted(glob.glob(f"{SRC}/{venue}/{st}/pair=SOLUSDT/date={day}/*.jsonl.gz"))
            if not files:
                continue
            d = f"{OUT}/{venue}/{st}/pair=SOLUSDT/date={day}"; os.makedirs(d, exist_ok=True)
            n = 0
            with gzip.open(f"{d}/slice.jsonl.gz", "wt") as w:
                for fp in files:
                    try:
                        with gzip.open(fp, "rt") as r:
                            for line in r:
                                try: ts = json.loads(line)[key]
                                except Exception: continue
                                if ts >= t1: break
                                if ts >= t0: w.write(line); n += 1
                    except (EOFError, OSError): pass
            print(day, venue, st, n, flush=True)
