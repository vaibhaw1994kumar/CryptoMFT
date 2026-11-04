"""Phase 1-lite STEP 0: classify each March-2024 day from the inventory (step0/<venue>.json) and the Bybit rebuild acceptance.

A minute is COVERED on a venue when (a) the venue's book stream has at least one row in that minute (Bybit: book_events; Binance: book_snapshots,
which are best bid/ask only) and (b) the trade tape is live: a trade within the minute, or trades within 5 minutes on both sides of it.
Two-venue overlap hours = minutes covered on BOTH venues / 60.
FULL: overlap >= 20 h, book and trades on both venues, no truncated file. PARTIAL: 4 h <= overlap < 20 h (or a truncated file). UNUSABLE: below 4 h.
Writes step0/classification.json."""
import json
from pathlib import Path

import numpy as np

E = Path(__file__).resolve().parent / "step0"
GAP_MIN = 5
inv = {v: json.loads((E / f"{v}.json").read_text())["days"] for v in ("bybit", "binance")}
BOOK = {"bybit": "book_events", "binance": "book_snapshots"}


def trade_live(counts: np.ndarray) -> np.ndarray:
    idx = np.flatnonzero(counts > 0)
    live = counts > 0
    if len(idx) < 2:
        return live
    for a, b in zip(idx[:-1], idx[1:]):
        if b - a <= GAP_MIN:
            live[a:b + 1] = True
    return live


out = {"rule": __doc__, "days": {}}
for day in sorted(set(inv["bybit"]) & set(inv["binance"])):
    cov, per = {}, {}
    for v in ("bybit", "binance"):
        d = inv[v][day]
        book = np.array(d[BOOK[v]]["minute_counts"]) > 0
        tr = trade_live(np.array(d["trades"]["minute_counts"]))
        cov[v] = book & tr
        per[v] = {"book_channel": BOOK[v], "book_rows": d[BOOK[v]]["rows"], "book_events": d["book_events"]["rows"],
                  "snapshots": d["book_snapshots"]["rows"], "trades": d["trades"]["rows"],
                  "truncated": [c for c in ("book_events", "book_snapshots", "trades") if d[c]["truncated"]],
                  "files": [{k: f[k] for k in ("path", "bytes", "sha256", "truncated", "rows", "gaps_over_5s", "gap_seconds", "backward_steps",
                                                "id_fields_in_first_2000", "snapshot_levels_first50")} for c in ("book_events", "book_snapshots", "trades")
                            for f in d[c]["files"]],
                  "book_minutes": int(book.sum()), "trade_live_minutes": int(tr.sum()), "covered_hours": round(float(cov[v].sum()) / 60, 2)}
    both = cov["bybit"] & cov["binance"]
    hours = float(both.sum()) / 60
    has_all = all(per[v]["book_rows"] > 0 and per[v]["trades"] > 0 for v in per)
    trunc = any(per[v]["truncated"] for v in per)
    cls = "FULL" if (hours >= 20 and has_all and not trunc) else ("PARTIAL" if hours >= 4 and has_all else "UNUSABLE")
    acc_f = E / "rebuild_acceptance" / f"bybit_{day}.json"
    acc = None
    if acc_f.exists():
        a = json.loads(acc_f.read_text())
        acc = {k: {m: v[m] for m in ("touch_price_match_pct", "touch_size_match_pct", "top10_level_match_pct") if m in v} for k, v in a["variants"].items()}
        acc["snapshots_compared"] = a.get("snapshots") or a.get("n_snapshots")
    out["days"][day] = {"class": cls, "overlap_hours": round(hours, 2), "venues": per, "bybit_rebuild_acceptance": acc,
                        "products": {"bybit": "Bybit linear USDT perp SOLUSDT (declared)", "binance": "Binance USD-M perp SOLUSDT (declared)"}}
    print(day, cls, f"{hours:5.2f} h", {v: (per[v]["book_rows"], per[v]["trades"], per[v]["truncated"]) for v in per})
(E / "classification.json").write_text(json.dumps(out, indent=1))
