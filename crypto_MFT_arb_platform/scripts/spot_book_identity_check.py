#!/usr/bin/env python3
"""Is data/bybit/ (2026-04) the SPOT market the real orders traded in? Each real maker execution (exec_spot, isMaker) is matched to a
recorded public trade at the identical price, on the opposite aggressor side, within +-0.25 s. A control repeats the match with the
execution times shifted by an hour. A linear (perpetual) book would match at about the control rate.

    python3 -I scripts/spot_book_identity_check.py --zip ~/Downloads/bybit_30d.zip --data-root ../data --days 2026-04-19:2026-04-25 --out config/spot_book_identity_check.json
"""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import json
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from new_backtest.event_loader import iter_venue_events  # noqa: E402

WIN = 0.25


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", required=True)
    ap.add_argument("--data-root", required=True)
    ap.add_argument("--days", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    d0, d1 = a.days.split(":")
    z = zipfile.ZipFile(a.zip)
    ex = defaultdict(list)
    for name in z.namelist():
        base = name.rsplit("/", 1)[-1]
        if base.startswith("exec_spot_"):
            sym = base[len("exec_spot_"):-len(".jsonl")]
            for line in z.open(name):
                r = json.loads(line)
                t = int(r["execTime"]) / 1000.0
                day = dt.datetime.fromtimestamp(t, tz=dt.timezone.utc).strftime("%Y-%m-%d")
                if r.get("isMaker") and r["execType"] == "Trade" and d0 <= day <= d1:
                    ex[(sym, day)].append((t, r["side"] == "Buy", float(r["execPrice"])))
    tot = {"executions": 0, "matched": 0, "control_matched": 0}
    per = {}
    for (sym, day), rows in sorted(ex.items()):
        tr = sorted((e.exchange_ts, float(e.payload["price"]), str(e.payload["side"]).lower().startswith("s"))
                    for e in iter_venue_events(a.data_root, "bybit", sym, day, day, include_snapshots=False, include_deltas=False))
        ts = [x[0] for x in tr]

        def hit(t, buy, px):
            i = bisect.bisect_left(ts, t - WIN)
            while i < len(tr) and tr[i][0] <= t + WIN:
                if abs(tr[i][1] - px) < 1e-9 and tr[i][2] == buy:        # our buy fills against an aggressor sell
                    return True
                i += 1
            return False
        m = sum(hit(t, b, p) for t, b, p in rows)
        c = sum(hit(t + 3600.0, b, p) for t, b, p in rows)
        per.setdefault(sym, {"executions": 0, "matched": 0, "control_matched": 0})
        for k, v in (("executions", len(rows)), ("matched", m), ("control_matched", c)):
            per[sym][k] += v
            tot[k] += v
    doc = {"schema": "spot_book_identity_check/v1", "window_s": WIN, "days": [d0, d1], "overall": tot, "by_symbol": per,
           "reading": "matched >> control means the recorded market is the one the orders traded in; unmatched executions fall where the "
                      "collector has gaps (see postonly_false_reject_check.json, stale_book)"}
    Path(a.out).write_text(json.dumps(doc, indent=1) + "\n")
    print(tot)
    return 0


if __name__ == "__main__":
    sys.exit(main())
