"""Phase D, D5 (owner 8 Nov): the cost of the core's stale-venue cancel rule, per day (a run wrapper). Usage: stale_cancel_cost.py OLD_RUNS NEW_RUNS DAY [DAY ...]
For each day: the maker quotes the session cancelled by the rule (not by the 15 s lifetime), the fills the SAME quotes got on the old path after the session cancelled them (matched by send time within 1 ms, side and price),
and those fills' markouts at 1, 5 and 30 s against the maker venue's mid (positive = price moved our way after the fill). Quotes with no old counterpart (the two runs had diverged by then) are counted, not guessed.
Markouts here use the exchange-time mid of the maker venue's own book (a replay of its events through the backtester's message-atomic book), an approximation of the run's receive-time markouts; they are labelled so.
EXPLORATORY; January is parity only, not a strategy result. Restricted to the common hours where both venues have data."""
import bisect
import json
import sys
from pathlib import Path

sys.path.insert(0, "/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/src")
from new_backtest.event_loader import BOOK, DELTA, delta_fields, iter_venue_events, snapshot_levels
from new_backtest.order_book import OrderBook

old_runs, new_runs, days = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3:]
H = (1.0, 5.0, 30.0)
TICK = {"2022": 0.005, "2024": 0.001}
ROOT = {"2022": "/Users/vaibhav/Documents/Newproject/data", "2024": "/Users/vaibhav/Documents/Newproject/out/queue_v2/slices_mar2024"}


def mids_at(root, venue, day, times):
    """The maker venue's mid at each requested time (exchange time), one pass through its events."""
    book = OrderBook(venue, "SOLUSDT", "hybrid", message_atomic=True)
    want = sorted(set(times))
    out, i = {}, 0
    for ev in iter_venue_events(root, venue, "SOLUSDT", day, day, include_trades=False):
        while i < len(want) and ev.exchange_ts > want[i]:
            out[want[i]] = book.mid if book.ready else None
            i += 1
        if i >= len(want):
            break
        if ev.kind == BOOK:
            bids, asks = snapshot_levels(ev.payload)
            if bids and asks:
                book.apply_snapshot(bids, asks, ev.exchange_ts)
        elif ev.kind == DELTA:
            side, price, vol, et = delta_fields(ev.payload)
            if price > 0:
                book.apply_delta(side, price, vol, et, ev.exchange_ts)
                book.end_message()
    return out


for day in days:
    o = old_runs / "g1o/base/perp_perp_maker_taker_v1_4" / day
    n = new_runs / "g1s/base/perp_perp_maker_taker_v1_5" / day
    ev = [json.loads(l) for l in (o / "order_events.jsonl").read_text().splitlines() if l.strip()]
    orders = [json.loads(l) for l in (n / "session_orders.jsonl").read_text().splitlines() if l.strip()]
    times = json.loads((n / "order_times.json").read_text())
    man = json.loads((n / "session_manifest.json").read_text())
    cov = man["manifest"]["coverage"]
    until = cov["common_window_ns"][1] / 1e9 if cov.get("common_window_ns") else 1e18
    tick = TICK[day[:4]]
    old_sends = [e for e in ev if e["kind"] == "order_sent" and e["venue"] == "bybit"]
    old_fills = [e for e in ev if e["kind"] == "fill" and e["venue"] == "bybit"]
    ruled, expired, other = [], 0, 0
    starts = sorted(a / 1e9 for a, b, c in man["manifest"]["invalid_book_policy"]["suspension_intervals_ns"])
    def after_a_window_start(ts):                       # the rule cancels when a window opens; the cancel lands within its latency (well under 0.5 s)
        i = bisect.bisect_right(starts, ts) - 1
        return i >= 0 and 0.0 <= ts - starts[i] <= 0.5
    for row, t in zip(orders, times):
        if row["venue"] != "BYBIT" or row["post_only"] is not True or t["send"] > until:
            continue
        if row["state"] == "CANCELLED":
            if row["lifetime_expired"]:
                expired += 1
            elif row["terminal_ts_ns"] and after_a_window_start(row["terminal_ts_ns"] / 1e9):
                ruled.append((row, t))
            else:
                other += 1
    matched, lost = [], []
    for row, t in ruled:
        cancel_ts = (row["terminal_ts_ns"] or 0) / 1e9
        c = [s for s in old_sends if abs(s["ts"] - t["send"]) <= 1e-3 and s["side"] == row["side"].lower() and abs(s["price"] - float(row["limit_price"])) <= 0.5 * tick]
        if not c:
            continue
        matched.append((row, c[0]))
        for f in old_fills:
            if f["signal_id"] == c[0]["signal_id"] and f["ts"] > cancel_ts:
                lost.append((row, f, cancel_ts))
    mids = mids_at(ROOT[day[:4]], "bybit", day, [f["ts"] + h for _, f, _ in lost for h in (0.0,) + H]) if lost else {}
    print(f"\n===== {day}  stale-venue cancel rule, maker venue Bybit (common hours only; excluded span: {[round(x['excluded_seconds']) for x in cov.get('excluded_span', [])]} s) =====")
    print(f"maker quotes sent {sum(1 for r, t in zip(orders, times) if r['venue'] == 'BYBIT' and r['post_only'] and t['send'] <= until)}; cancelled by the rule {len(ruled)}; by the 15 s lifetime {expired}; other cancels {other}")
    print(f"rule-cancelled quotes with an old-path counterpart {len(matched)}; with none (runs had diverged) {len(ruled) - len(matched)}")
    print(f"fills those quotes got on the old path after the session cancelled them: {len(lost)}  (qty {sum(f['qty'] for _, f, _ in lost):.4f} SOL)")
    for row, f, cts in lost:
        mk = []
        for h in H:
            m0, m1 = mids.get(f["ts"] + 0.0), mids.get(f["ts"] + h)
            if m0 and m1:
                sign = 1.0 if f["side"] == "buy" else -1.0
                bps = sign * (m1 - f["price"]) / f["price"] * 1e4
                mk.append(f"{h:g}s {bps:+.2f} bps ({bps * f['price'] * f['qty'] / 1e4:+.4f} USDT)")
            else:
                mk.append(f"{h:g}s n/a")
        print(f"   quote {row['side']} {row['limit_price']} cancelled at {cts:.3f}; old fill {f['qty']:.4f} @ {f['price']} at {f['ts']:.3f}; markout " + ", ".join(mk))
