"""Day-1 end-to-end replay, step B4 (a run wrapper, no logic of its own): did the day offer edge? Cross-venue mid gap and touch edges from the SAME recorded events and the SAME core books as the replay,
sampled once per second while both books are VALID and each venue's last book message is at most 1 s old (the recorder's own max_book_age). READ ONLY on data/.
Per hour (UTC) and for the day: signed mid gap (Bybit mid - Binance mid, bps of the mean mid), its absolute value, and the two maker-at-the-touch gross edges the strategy trades
  A = Binance best bid - Bybit best bid   (buy as maker on Bybit at its bid, sell on Binance at its bid)
  B = Bybit best ask - Binance best ask   (sell as maker on Bybit at its ask, buy on Binance at its ask)
in bps of the mean mid, median / p99 / max, and the share of seconds at or above the bar: fees (Bybit maker + Binance taker, from the run's fee rows) + the profile's min_net_edge_bps, BEFORE the expected hedge move.
usage: mid_gap.py OUT_JSON"""
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, "/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/src")
from crypto_stat_arb.orderbook import BookState, OrderBook
from new_backtest.session_replay import iter_recorded_events

SESSION = Path("/Users/vaibhav/Documents/Newproject/data/live_sessions/solusdt/2026-10-29/SOLUSDT/e9b81728a3054812a48035a1b3e02d42")
MAX_AGE_NS = 1_000_000_000
FEES_BPS = 1.0 + 1.7            # Bybit maker 1.0 + Binance taker 1.7 (session_fills of the v1_6 run: fee / notional)
MIN_NET_BPS = 1.0               # profiles/perp_perp_maker_taker_v1_6.yaml arb.min_net_edge_bps
BAR_BPS = FEES_BPS + MIN_NET_BPS


def q(v, p):
    v = sorted(v)
    return v[min(len(v) - 1, int(len(v) * p))]


def block(rows):
    out = {"seconds": len(rows)}
    for name, i in (("gap_bps_signed", 0), ("gap_bps_abs", 1), ("edge_A_bps", 2), ("edge_B_bps", 3)):
        v = [r[i] for r in rows]
        out[name] = {"median": q(v, 0.5), "p1": q(v, 0.01), "p99": q(v, 0.99), "max": max(v), "min": min(v)}
    out["share_A_at_or_above_bar"] = sum(r[2] >= BAR_BPS for r in rows) / len(rows)
    out["share_B_at_or_above_bar"] = sum(r[3] >= BAR_BPS for r in rows) / len(rows)
    out["share_A_or_B_positive"] = sum(max(r[2], r[3]) > 0 for r in rows) / len(rows)
    return out


books: dict[str, OrderBook] = {}
last_ns: dict[str, int] = {}
by_hour, day = defaultdict(list), []
skipped = {"not_valid": 0, "stale": 0, "no_side": 0}
next_ns = None
t = time.time()
for ev in iter_recorded_events(SESSION):
    if not hasattr(ev, "bids"):                       # a public print: not a book message, the book's age is read from book messages only
        continue
    v = ev.venue
    book = books.setdefault(v, OrderBook(v, ev.instrument_id))
    book.apply(ev)
    last_ns[v] = ev.receive_ts_ns
    now = ev.receive_ts_ns
    if next_ns is None:
        next_ns = now - now % 1_000_000_000 + 1_000_000_000
    if now < next_ns or len(books) < 2:
        continue
    next_ns = now - now % 1_000_000_000 + 1_000_000_000
    by, bn = books.get("BYBIT"), books.get("BINANCE")
    if by is None or bn is None:
        continue
    if by.state != BookState.VALID or bn.state != BookState.VALID:
        skipped["not_valid"] += 1
        continue
    if now - last_ns["BYBIT"] > MAX_AGE_NS or now - last_ns["BINANCE"] > MAX_AGE_NS:
        skipped["stale"] += 1
        continue
    if not (by.bids and by.asks and bn.bids and bn.asks):
        skipped["no_side"] += 1
        continue
    bb, ba, nb, na = float(by.best_bid), float(by.best_ask), float(bn.best_bid), float(bn.best_ask)
    mid = ((bb + ba) / 2 + (nb + na) / 2) / 2
    gap = ((bb + ba) / 2 - (nb + na) / 2) / mid * 1e4
    row = (gap, abs(gap), (nb - bb) / mid * 1e4, (ba - na) / mid * 1e4)
    day.append(row)
    by_hour[time.strftime("%H", time.gmtime(now / 1e9))].append(row)

res = {"label": "EXPLORATORY: cross-venue mid gap and maker-at-the-touch gross edges, SOLUSDT perps, 2026-10-29 (recorder quality REJECTED; host clock UNKNOWN; receive-time clock)",
       "method": __doc__.split("usage:")[0].strip(), "bar_bps": BAR_BPS, "fees_bps": FEES_BPS, "min_net_edge_bps": MIN_NET_BPS, "max_book_age_ns": MAX_AGE_NS, "skipped_seconds": skipped,
       "day": block(day), "by_hour_utc": {h: block(r) for h, r in sorted(by_hour.items())}, "wall_s": round(time.time() - t, 1)}
Path(sys.argv[1]).write_text(json.dumps(res, indent=1))
print(json.dumps({"seconds": len(day), "skipped": skipped, "wall_s": res["wall_s"], "day": res["day"]}, indent=1))
