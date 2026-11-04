"""Turn a fillprobe run (real perpetual orders + the capture they rested in) into the backtester's calibration format.

Writes `fill_events.jsonl` rows with exactly the keys `new_backtest.fill_label.extract_labels` reads, one row per (order, horizon):
timestamp, symbol, side, horizon_s, filled, relative_to_touch_ticks, queue_ahead, local_depth, local_depth_baseline, spread_pct,
short_vol, trade_count, trade_sign, regime_bucket, terminal_reason, fill_ratio, price_moved_through, observation_time_s.

The features are computed from the run's own capture, replayed in exchange-time order, with the same definitions as
new_backtest.venue_state (EWMA alpha 0.05 over mid returns for short_vol, alpha 0.02 over top-of-book depth for the baseline,
2 s flow window for trade_count / trade_sign). regime_bucket is an APPROXIMATION: the backtester's bucket comes from a price-impact
regime classifier that needs its own history; here it is the run-wide quantile of short_vol (<= 65% calm, > 90% toxic, else
elevated) and is labelled as such in the sidecar. No order ids or credentials are written.

Only orders that rested are informative: a stream/REST rejection never queued and is skipped. An order cancelled before a horizon
without filling is written with observation_time_s < horizon_s and terminal_reason "cancel", which extract_labels treats as censored.
"""
from __future__ import annotations

import gzip
import json
import math
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

VOL_ALPHA, DEPTH_ALPHA, FLOW_WINDOW_S = 0.05, 0.02, 2.0
HORIZONS = (1, 5, 15, 30)
VENUE_OF = {"bybit_linear": "bybit", "binance_futures": "binance"}


def _rows(path: Path):
    try:
        with gzip.open(path, "rt") as fh:
            for line in fh:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except (EOFError, OSError):
        return


def load_capture(cap: Path, venue: str, symbol: str) -> tuple[list, list, list]:
    """(snapshots, book events, trades) for one venue/symbol over the whole run, each sorted by exchange time."""
    base = cap / venue
    snaps = [r for f in sorted((base / "book_snapshots" / f"pair={symbol}").glob("date=*/*.jsonl.gz")) for r in _rows(f)]
    evs = [r for f in sorted((base / "book_events" / f"pair={symbol}").glob("date=*/*.jsonl.gz")) for r in _rows(f)]
    trs = [r for f in sorted((base / "trades" / f"pair={symbol}").glob("date=*/*.jsonl.gz")) for r in _rows(f)]
    snaps.sort(key=lambda r: r["timestamp"])
    evs.sort(key=lambda r: r["exchange_ts"])
    trs.sort(key=lambda r: r["exchange_ts"])
    return snaps, evs, trs


class FeatureReplay:
    """Replays snapshots + level updates + trades in time order and answers the backtester's feature questions at any time."""

    def __init__(self, snaps, evs, trs):
        self.items = sorted([("s", r["timestamp"], r) for r in snaps] + [("e", r["exchange_ts"], r) for r in evs] + [("t", r["exchange_ts"], r) for r in trs],
                            key=lambda x: (x[1], {"s": 0, "e": 1, "t": 2}[x[0]]))
        self.i = 0
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.mid = 0.0
        self.short_vol = 0.0
        self.depth_baseline = 0.0
        self.flow: deque = deque()          # (ts, signed volume, volume)
        self.vol_history: list[float] = []

    def _top(self):
        b = max(self.bids) if self.bids else 0.0
        a = min(self.asks) if self.asks else 0.0
        return b, a, self.bids.get(b, 0.0), self.asks.get(a, 0.0)

    def _after_book_change(self):
        b, a, bs, as_ = self._top()
        if b > 0 and a > b:
            mid = (a + b) / 2
            if self.mid > 0 and mid != self.mid:
                ret = mid / self.mid - 1.0
                self.short_vol = math.sqrt(VOL_ALPHA * ret * ret + (1 - VOL_ALPHA) * self.short_vol ** 2)
                self.vol_history.append(self.short_vol)
            self.mid = mid
            depth = bs + as_
            self.depth_baseline = depth if self.depth_baseline <= 0 else DEPTH_ALPHA * depth + (1 - DEPTH_ALPHA) * self.depth_baseline

    def advance(self, ts: float) -> None:
        while self.i < len(self.items) and self.items[self.i][1] <= ts:
            kind, t, r = self.items[self.i]
            self.i += 1
            if kind == "s":
                self.bids = {float(p): float(q) for p, q in r["bids"]}
                self.asks = {float(p): float(q) for p, q in r["asks"]}
                self._after_book_change()
            elif kind == "e":
                d = self.bids if r["side"] == "bid" else self.asks
                if r["volume"] <= 0:
                    d.pop(float(r["price"]), None)
                else:
                    d[float(r["price"])] = float(r["volume"])
                self._after_book_change()
            else:
                v = float(r["volume"])
                self.flow.append((t, v if r["side"] == "b" else -v, v))
        while self.flow and self.flow[0][0] < ts - FLOW_WINDOW_S:
            self.flow.popleft()

    def features(self, ts: float) -> dict:
        self.advance(ts)
        b, a, bs, as_ = self._top()
        tot = sum(x[2] for x in self.flow)
        return {"best_bid": b, "best_ask": a, "local_depth": bs + as_, "local_depth_baseline": self.depth_baseline if self.depth_baseline > 0 else 1.0,
                "spread_pct": ((a - b) / ((a + b) / 2) * 100.0) if b > 0 and a > b else 0.0, "short_vol": self.short_vol,
                "trade_count": len(self.flow) / FLOW_WINDOW_S, "trade_sign": (sum(x[1] for x in self.flow) / tot) if tot > 0 else 0.0}

    def moved_through(self, side: str, price: float, t0: float, t1: float, trades) -> bool:
        """A print on our side of the book beyond our price inside (t0, t1]: a sell below our bid, or a buy above our ask."""
        for r in trades:
            if r["exchange_ts"] <= t0:
                continue
            if r["exchange_ts"] > t1:
                break
            if side == "bid" and r["side"] == "s" and r["price"] < price - 1e-12:
                return True
            if side == "ask" and r["side"] == "b" and r["price"] > price + 1e-12:
                return True
        return False


def regime_from_vol(vol: float, cuts: tuple[float, float]) -> str:
    calm, toxic = cuts
    return "calm" if vol <= calm else ("toxic" if vol > toxic else "elevated")


def _quantile(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    return s[min(len(s) - 1, int(q * len(s)))]


def convert_run(run: Path, out: Path | None = None, horizons=HORIZONS) -> dict:
    """Write <run>/calibration/fill_events.jsonl (+ provenance sidecar). Returns a summary (counts per market and horizon)."""
    run = Path(run)
    out = Path(out) if out else run / "calibration" / "fill_events.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    meta = json.loads((run / "fillprobe_meta.json").read_text()) if (run / "fillprobe_meta.json").exists() else {}
    summary = {"run": str(run), "network": meta.get("network"), "markets": {}, "rows": 0, "regime_bucket_source": "run-wide short_vol quantiles (approximation)"}
    rows_out = []
    for d in sorted(run.glob("*_20*Z")):
        market = d.name.rsplit("_", 1)[0]
        if market not in VENUE_OF or not (d / "orders.jsonl").exists():
            continue
        venue = VENUE_OF[market]
        orders = [json.loads(x) for x in (d / "orders.jsonl").read_text().splitlines() if x.strip()]
        orders = [o for o in orders if o.get("kind") == "fill_probe" and not o["outcome"].startswith("rejected") and (o["ex"].get("created_ms") or o["ex"].get("rest_ms"))]
        by_sym: dict[str, list] = {}
        for o in orders:
            by_sym.setdefault(o["symbol"], []).append(o)
        mstat = {"orders": 0, "rows": 0, "filled_within": {str(h): 0 for h in horizons}, "censored": {str(h): 0 for h in horizons}}
        for sym, os_ in by_sym.items():
            snaps, evs, trs = load_capture(run / "capture", venue, sym)
            if not snaps:
                continue
            rp = FeatureReplay(snaps, evs, trs)
            feats = []
            for o in sorted(os_, key=lambda o: o["ex"].get("created_ms") or o["ex"]["rest_ms"]):
                created = (o["ex"].get("created_ms") or o["ex"]["rest_ms"]) / 1000.0
                f = rp.features(created)
                feats.append((o, created, f))
            cuts = (_quantile(rp.vol_history, 0.65), _quantile(rp.vol_history, 0.90))
            for o, created, f in feats:
                side = "bid" if o["side"] == "Buy" else "ask"
                price, qty, filled = float(o["price"]), float(o["qty"]), float(o.get("filled_qty") or 0.0)
                first = o["first_fill_ex_ms"] / 1000.0 if o.get("first_fill_ex_ms") else None
                last = o["last_fill_ex_ms"] / 1000.0 if o.get("last_fill_ex_ms") else None
                cancel_ts = o["ex"]["wscan_event_ms"] / 1000.0 if o["ex"].get("wscan_event_ms") else None
                full = filled >= qty - 1e-12
                if full and last is not None:
                    end, terminal = last, "full_fill"
                elif filled > 0:
                    end, terminal = (cancel_ts or last or created), "partial_then_cancel"
                else:
                    end, terminal = (cancel_ts or created + float(o.get("hold_s") or 0.0)), "cancel"
                obs = max(0.0, end - created)
                queue = o.get("size_at_price_at_send")
                mstat["orders"] += 1
                for h in horizons:
                    hit = first is not None and first - created <= h
                    if not hit and terminal == "cancel" and obs < h:
                        mstat["censored"][str(h)] += 1
                    if hit:
                        mstat["filled_within"][str(h)] += 1
                    rows_out.append({
                        "timestamp": created, "symbol": sym, "side": side, "horizon_s": h, "filled": bool(hit),
                        "relative_to_touch_ticks": 0, "queue_ahead": float(queue) if queue is not None else 0.0,
                        "local_depth": f["local_depth"], "local_depth_baseline": f["local_depth_baseline"], "spread_pct": f["spread_pct"],
                        "short_vol": f["short_vol"], "trade_count": f["trade_count"], "trade_sign": f["trade_sign"],
                        "regime_bucket": regime_from_vol(f["short_vol"], cuts), "terminal_reason": terminal,
                        "fill_ratio": min(1.0, filled / qty) if qty > 0 else 0.0,
                        "price_moved_through": rp.moved_through(side, price, created, created + h, trs),
                        "observation_time_s": obs, "level_was_absent": queue is None,
                        "market": market, "network": meta.get("network", ""), "source": "ollog_fillprobe", "order_size": qty,
                    })
                    mstat["rows"] += 1
        summary["markets"][market] = mstat
    rows_out.sort(key=lambda r: (r["timestamp"], r["symbol"], r["horizon_s"]))
    with out.open("w", encoding="utf-8") as fh:
        for r in rows_out:
            fh.write(json.dumps(r, sort_keys=True) + "\n")
    summary["rows"] = len(rows_out)
    summary["written"] = str(out)
    summary["label_matured_until_ts"] = max((r["timestamp"] + r["horizon_s"] for r in rows_out), default=0.0)
    summary["generated_utc"] = datetime.now(timezone.utc).isoformat()
    (out.parent / "fill_events_provenance.json").write_text(json.dumps(summary, indent=1))
    return summary


def add_arguments(s) -> None:
    s.add_argument("--run", required=True, help="a fillprobe_* run directory")
    s.add_argument("--out", default=None, help="output jsonl (default <run>/calibration/fill_events.jsonl)")


def main(args) -> int:
    s = convert_run(Path(args.run), Path(args.out) if args.out else None)
    for m, st in s["markets"].items():
        print(f"{m}: {st['orders']} orders -> {st['rows']} rows; filled within h: {st['filled_within']}; censored: {st['censored']}")
    print(f"rows {s['rows']} -> {s['written']} (labels mature by {s['label_matured_until_ts']:.0f}); regime_bucket: {s['regime_bucket_source']}")
    return 0
