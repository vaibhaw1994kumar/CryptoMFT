"""End-to-end self-test on a manufactured second venue.

There is no Binance data on disk yet, so the cross-venue path — receive-time
merge, in-flight latency, taker walk, hedge dispatch, leg-risk unwind, PnL
reconciliation — cannot be exercised against real data. This builds a fixture:

    venue A  "bybit"    real records from data/bybit/, unchanged
    venue B  "binance"  the SAME records, prices scaled by a factor that is 1.0
                        everywhere except inside short injected windows where
                        B is dislocated by a known number of bps

At each window boundary B receives a full scaled snapshot, so its book is
internally coherent — scaling only the deltas would leave pre-boundary resting
levels unscaled, the book would cross itself, and the engine's own repair would
(correctly) delete the dislocation being tested.

Outside the windows the two venues quote the same price, so there is nothing to
arbitrage and the strategy must stay flat. Inside them the edge is known
exactly. That gives testable invariants that a real dataset cannot:

    * no fill lands outside a dislocation window (+ latency slack)
    * PnL reconciles: mark_to_market == sum(cash) + sum(inventory x mid)
    * every completed signal is hedged, unhedged-and-flattened, or both-missed
    * gross edge capture is <= 1
    * windows are short (default 120 ms) so latency bites: the co-located
      preset must capture more of them than the Singapore preset, whose
      Binance leg alone is ~40 ms each way
    * the toxicity gate suppresses a flow-less repricing when enabled

Venue B carries no local_ts, so it exercises the modelled-latency path while A
exercises the measured one — the two clocks have to line up for any of this to
pass.

This is a fixture, not a result. Nothing here says anything about whether
Bybit x Binance arb is profitable. It says whether the engine is honest.

    python -m new_backtest.selftest [--pair SOLUSDT --day 2026-04-16 --events 150000]
"""
from __future__ import annotations

import argparse
import gzip
import json
import shutil
import tempfile
from pathlib import Path

from . import virtual_clock
from .arb_strategy import MAKER_TAKER, TAKER_TAKER, ArbConfig
from .backtester import ArbBacktestConfig, CrossVenueArbBacktester
from .event_loader import BOOK, DELTA, TRADE, iter_venue_events


# ── Fixture ───────────────────────────────────────────────────────────────────

def _scale_levels(levels, f: float):
    return [[float(p) * f, float(s)] for p, s in levels if p and s]


def build_fixture(
    src_root: Path, pair: str, day: str, n_events: int, out_root: Path,
    n_windows: int = 20, window_s: float = 0.12, dislocation_bps: float = 25.0,
) -> list[tuple[float, float, float]]:
    """Copy `n_events` real Bybit records; write them as venue A and as a
    dislocated venue B. Returns the (start, end, bps) windows injected."""
    events = []
    for ev in iter_venue_events(src_root, "bybit", pair, day, day):
        events.append(ev)
        if len(events) >= n_events:
            break
    if len(events) < 1000:
        raise SystemExit(f"fixture: only {len(events)} events found for {pair} {day}")

    t0, t1 = events[0].exchange_ts, events[-1].exchange_ts
    span = t1 - t0
    windows: list[tuple[float, float, float]] = []
    for i in range(n_windows):
        start = t0 + span * (0.10 + 0.80 * i / max(1, n_windows - 1))
        sign = 1.0 if i % 2 == 0 else -1.0
        windows.append((start, start + window_s, sign * dislocation_bps))

    def factor(ts: float) -> float:
        for s, e, bps in windows:
            if s <= ts <= e:
                return 1.0 + bps / 10_000.0
        return 1.0

    writers: dict[tuple[str, str], gzip.GzipFile] = {}

    def w(venue: str, stream: str):
        key = (venue, stream)
        if key not in writers:
            d = out_root / venue / stream / f"pair={pair}" / f"date={day}"
            d.mkdir(parents=True, exist_ok=True)
            writers[key] = gzip.open(d / f"{int(t0 * 1000)}.jsonl.gz", "wt")
        return writers[key]

    stream_of = {BOOK: "book_snapshots", DELTA: "book_events", TRADE: "trades"}

    def scaled_snapshot(p: dict, f: float, ts: float) -> dict:
        q = dict(p)
        q["exchange"] = "binance"
        q["timestamp"] = ts
        q.pop("local_ts", None)
        q.pop("clock_drift", None)
        q["bids"] = _scale_levels(p.get("bids", []), f)
        q["asks"] = _scale_levels(p.get("asks", []), f)
        q["best_bid"] = float(p["best_bid"]) * f
        q["best_ask"] = float(p["best_ask"]) * f
        return q

    last_snapshot: dict | None = None
    prev_f = 1.0
    try:
        for ev in events:
            p = ev.payload
            w("bybit", stream_of[ev.kind]).write(json.dumps(p, separators=(",", ":")) + "\n")
            if ev.kind == BOOK:
                last_snapshot = p

            f = factor(ev.exchange_ts)
            if f != prev_f and last_snapshot is not None and ev.kind != BOOK:
                w("binance", "book_snapshots").write(
                    json.dumps(scaled_snapshot(last_snapshot, f, ev.exchange_ts - 1e-4),
                               separators=(",", ":")) + "\n")
            prev_f = f

            if ev.kind == BOOK:
                q = scaled_snapshot(p, f, float(p["timestamp"]))
            else:
                q = dict(p)
                q["exchange"] = "binance"
                q.pop("local_ts", None)       # force the modelled-latency path
                q.pop("clock_drift", None)
                q["price"] = float(p["price"]) * f
            w("binance", stream_of[ev.kind]).write(json.dumps(q, separators=(",", ":")) + "\n")
    finally:
        for fh in writers.values():
            fh.close()
    return windows


# ── Checks ────────────────────────────────────────────────────────────────────

class Check:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passes = 0

    def __call__(self, cond: bool, msg: str) -> None:
        if cond:
            self.passes += 1
            print(f"    ok   {msg}")
        else:
            self.failures.append(msg)
            print(f"    FAIL {msg}")


def _run(root: Path, pair: str, day: str, mode: str, preset: str, out: Path,
         warm: str | None = None, block_toxic: bool = False):
    virtual_clock.reset(0.0)
    cfg = ArbBacktestConfig(
        pair=pair, data_root=str(root), venues=("bybit", "binance"),
        start_date=day, end_date=day, latency_preset=preset,
        arb=ArbConfig(mode=mode, min_net_edge_bps=1.0, max_book_age_s=0.5,
                      max_clip_notional=5_000.0, block_on_toxic=block_toxic),
        output_dir=str(out), warm_start_fill_events=warm, seed=11,
    )
    return CrossVenueArbBacktester(cfg).run()


def _fills_in_windows(trades_path: str, windows, slack_s: float) -> tuple[int, int]:
    inside = outside = 0
    with open(trades_path, encoding="utf-8") as fh:
        for line in fh:
            ts = json.loads(line)["ts"]
            if any(s - slack_s <= ts <= e + slack_s for s, e, _ in windows):
                inside += 1
            else:
                outside += 1
    return inside, outside


def _describe(r) -> None:
    fr = r.feed_report
    hc = fr["_host_clock"]
    sr = r.strategy_report
    rej = sorted(((k[7:], int(v)) for k, v in sr.items() if k.startswith("reject_") and v),
                 key=lambda x: -x[1])
    print(f"    events={r.events_processed:,}  signals={r.signals_generated}  "
          f"filled={r.orders_filled}/{r.orders_sent}  missed={r.orders_missed} {dict(r.miss_reasons) or ''}")
    print(f"    hedged={r.signals_fully_hedged} unhedged={r.signals_unhedged} both_missed={r.signals_both_missed}  "
          f"pnl={r.realized_pnl:,.2f}  gross_capture={r.edge_capture_ratio:.2f}  "
          f"net_edge={r.mean_realized_net_edge_bps:.1f}bps  slip={r.mean_slippage_bps:.2f}bps")
    print(f"    host clock {hc['host_minus_exchange_ms']:+.1f}ms (accepted {int(hc['accepted_samples'])}, "
          f"backlogged {int(hc['rejected_backlogged'])}, reseeds {int(hc['reseeds'])});  "
          f"feed latency vs host: bybit {fr['bybit']['mean_feed_latency_vs_host_ms']:.1f}ms  "
          f"binance {fr['binance']['mean_feed_latency_vs_host_ms']:.1f}ms")
    print(f"    evaluations={int(sr['evaluations'])} best_net_edge={sr['best_net_edge_bps']:.1f}bps  "
          f"rejects={rej[:5]}  crossed={int(r.book_stats['bybit']['crossed_events'])}"
          f"/{int(r.book_stats['binance']['crossed_events'])}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src-root", default="data")
    ap.add_argument("--pair", default="SOLUSDT")
    ap.add_argument("--day", default="2026-04-16")
    ap.add_argument("--events", type=int, default=150_000)
    ap.add_argument("--windows", type=int, default=20)
    ap.add_argument("--window-ms", type=float, default=120.0)
    ap.add_argument("--dislocation-bps", type=float, default=25.0)
    ap.add_argument("--keep", action="store_true", help="keep the fixture dir")
    args = ap.parse_args(argv)

    tmp = Path(tempfile.mkdtemp(prefix="nb_selftest_"))
    check = Check()
    try:
        print(f"[fixture] {args.pair} {args.day} first {args.events:,} events -> {tmp}")
        windows = build_fixture(Path(args.src_root), args.pair, args.day, args.events, tmp,
                                n_windows=args.windows, window_s=args.window_ms / 1000.0,
                                dislocation_bps=args.dislocation_bps)
        print(f"[fixture] {len(windows)} dislocation windows of {args.window_ms:.0f} ms at ±{args.dislocation_bps:.0f} bps")

        results = {}
        for preset in ("colocated_both", "singapore"):
            print(f"\n[taker_taker / {preset} / toxic gate off]")
            r = _run(tmp, args.pair, args.day, TAKER_TAKER, preset, tmp / f"tt_{preset}")
            results[preset] = r
            _describe(r)

            check(r.signals_generated > 0, "signals fired inside dislocations")
            recon = sum(r.final_cash.values()) + sum(
                r.final_inventory[v] * r.final_mids.get(v, 0.0) for v in r.final_inventory)
            check(abs(r.mark_to_market_pnl - recon) < 1e-6,
                  f"mark-to-market = cash + inventory x mid ({r.mark_to_market_pnl:.4f} vs {recon:.4f})")
            check(abs(r.net_base_position) < 1e-6,
                  f"net base position flat after unwinds ({r.net_base_position:.3e})")
            if r.trades_path:
                inside, outside = _fills_in_windows(r.trades_path, windows, slack_s=0.25)
                check(outside == 0, f"no fills outside dislocation windows (inside={inside}, outside={outside})")
            check(r.edge_capture_ratio <= 1.02,
                  f"gross edge capture ≤ 1 (got {r.edge_capture_ratio:.3f})")
            check(r.signals_completed == r.signals_fully_hedged + r.signals_unhedged + r.signals_both_missed,
                  f"completed {r.signals_completed} = hedged {r.signals_fully_hedged} + unhedged "
                  f"{r.signals_unhedged} + both-missed {r.signals_both_missed}")

        co, sg = results["colocated_both"], results["singapore"]
        print("\n[latency sensitivity]")
        print(f"    colocated: hedged={co.signals_fully_hedged} missed_orders={co.orders_missed} "
              f"capture={co.edge_capture_ratio:.3f} pnl={co.realized_pnl:,.2f}")
        print(f"    singapore: hedged={sg.signals_fully_hedged} missed_orders={sg.orders_missed} "
              f"capture={sg.edge_capture_ratio:.3f} pnl={sg.realized_pnl:,.2f}")
        check(co.realized_pnl > 0, "colocated_both is profitable on a 25 bps dislocation")
        check(sg.orders_missed > co.orders_missed
              or sg.signals_fully_hedged < co.signals_fully_hedged
              or sg.realized_pnl < co.realized_pnl,
              "more latency -> more misses, fewer hedged signals, or lower PnL")

        print(f"\n[toxic gate / colocated_both / gate on]")
        g = _run(tmp, args.pair, args.day, TAKER_TAKER, "colocated_both", tmp / "tt_gate", block_toxic=True)
        _describe(g)
        check(g.signals_generated < co.signals_generated,
              f"toxic gate suppresses flow-less repricing ({g.signals_generated} < {co.signals_generated} signals)")

        print(f"\n[maker_taker / singapore / two-pass]")
        m1 = _run(tmp, args.pair, args.day, MAKER_TAKER, "singapore", tmp / "mt_pass1")
        print(f"    pass1 signals={m1.signals_generated} maker_placed={int(m1.maker_stats['maker_orders_placed'])} "
              f"maker_filled={int(m1.maker_stats['maker_orders_filled'])} hedged={m1.signals_fully_hedged} "
              f"unhedged={m1.signals_unhedged} pnl={m1.realized_pnl:,.2f}")
        check(m1.maker_stats["maker_orders_placed"] > 0, "passive orders were placed")
        check(m1.fill_log_path is not None and Path(m1.fill_log_path).stat().st_size > 0,
              "EmpiricalFillModel event log written")
        m2 = _run(tmp, args.pair, args.day, MAKER_TAKER, "singapore", tmp / "mt_pass2", warm=m1.fill_log_path)
        print(f"    pass2 (warm) signals={m2.signals_generated} "
              f"maker_filled={int(m2.maker_stats['maker_orders_filled'])} pnl={m2.realized_pnl:,.2f}")
        check(m2.events_processed == m1.events_processed, "pass 2 replayed the same stream")

        print(f"\n{check.passes} passed, {len(check.failures)} failed")
        for f in check.failures:
            print(f"  - {f}")
        return 1 if check.failures else 0
    finally:
        if args.keep:
            print(f"[fixture kept] {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
