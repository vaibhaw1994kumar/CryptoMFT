"""Replay a fillprobe run through the maker fill engine under several configs and score simulated against real fills.

Method (same injection path as the fill-evidence study's fe_replay.py): the capture is loaded through new_backtest's own event
loader; each real probe order is injected at its exchange-created time with zero entry latency, its queue position comes from the
REPLAYED book, and it is cancelled at the real end time (full fill, or the exchange's cancel event). Scoring follows the study:
over real-filled orders, hit = simulated first fill within +-1 s of the real first fill, premature = more than 1 s earlier,
late = more than 1 s later, unfilled = never filled in the simulation; over real-unfilled orders, false_fill = the simulation
filled an order that really did not.

This module imports new_backtest from `--src` (a directory that contains the package), so a stable snapshot can be used while
other work edits the live tree. It never writes to data/: the capture directory is read-only here and results go to the run dir.
On testnet the output is a PLUMBING and event-ordering check; testnet queues are not mainnet queues.
"""
from __future__ import annotations

import collections
import json
import math
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path

VENUE_OF = {"bybit_linear": "bybit", "binance_futures": "binance"}
CONFIGS = {
    "off": {},
    "vanish_clears_queue": {"vanish_clears_queue": True},
    "confirm_delta_20ms": {"fill_evidence": "confirm_delta", "evidence_window_ms": 20.0},
    "vanish+confirm_delta_20ms": {"vanish_clears_queue": True, "fill_evidence": "confirm_delta", "evidence_window_ms": 20.0},
    # diagnostic, not a candidate: a vanished level (and any unexplained decrease) fills only when a real trade at the price confirms it
    "trade_confirm_20ms": {"fill_evidence": "trade_confirm", "evidence_window_ms": 20.0},
}
DEFAULT_CONFIGS = ("off", "vanish_clears_queue", "confirm_delta_20ms", "vanish+confirm_delta_20ms", "trade_confirm_20ms")
TOL_S = 1.0


def add_arguments(s) -> None:
    s.add_argument("--run", required=True, help="a fillprobe_* run directory")
    s.add_argument("--src", required=True, help="directory containing the new_backtest package (a snapshot of crypto_MFT_arb_platform/src)")
    s.add_argument("--configs", default=",".join(DEFAULT_CONFIGS), help=f"comma list of {list(CONFIGS)}")
    s.add_argument("--grace-s", type=float, default=0.0, help="extra seconds an order lives past its real end before the simulated cancel")
    s.add_argument("--out", default=None, help="results file (default: <run>/replay_results.json)")


# ---- real orders -----------------------------------------------------------------------------------------------------
def load_orders(run: Path) -> list[dict]:
    out = []
    for d in sorted(run.glob("*_20*Z")):
        market = d.name.rsplit("_", 1)[0]
        if market not in VENUE_OF or not (d / "orders.jsonl").exists():
            continue
        for line in (d / "orders.jsonl").read_text().splitlines():
            r = json.loads(line)
            if r.get("kind") != "fill_probe" or r["outcome"].startswith("rejected"):
                continue
            ex = r["ex"]
            created_ms = ex.get("created_ms") or ex.get("rest_ms")
            if not created_ms:
                continue
            qty, filled = float(r["qty"]), float(r.get("filled_qty") or 0)
            created = created_ms / 1000
            first = r["first_fill_ex_ms"] / 1000 if r.get("first_fill_ex_ms") else None
            last = r["last_fill_ex_ms"] / 1000 if r.get("last_fill_ex_ms") else None
            if filled >= qty - 1e-12 and last is not None:
                end = last
            elif ex.get("wscan_event_ms"):
                end = ex["wscan_event_ms"] / 1000
            else:
                end = created + float(r.get("hold_s") or 0)
            day = datetime.fromtimestamp(created, timezone.utc).strftime("%Y-%m-%d")
            out.append({"market": market, "venue": VENUE_OF[market], "symbol": r["symbol"], "day": day, "order_id": r["client_id"],
                        "side": r["side"], "price": float(r["price"]), "qty": qty, "created": created, "real_filled": filled,
                        "first_fill": first, "last_fill": last, "end": max(end, created), "outcome": r["outcome"],
                        "queue_at_send": r.get("size_at_price_at_send")})
    return out


# ---- simulation ----------------------------------------------------------------------------------------------------------
def replay_one(nb, data_root: str, venue: str, symbol: str, day: str, orders: list[dict], maker_kw: dict, grace_s: float) -> list[dict]:
    ArbBacktestConfig, CrossVenueArbBacktester = nb["ArbBacktestConfig"], nb["CrossVenueArbBacktester"]
    ArbConfig, ArbLeg, ArbSignal, MakerConfig = nb["ArbConfig"], nb["ArbLeg"], nb["ArbSignal"], nb["MakerConfig"]
    BOOK, snapshot_levels = nb["BOOK"], nb["snapshot_levels"]
    orders = sorted(orders, key=lambda o: o["created"])
    cfg = ArbBacktestConfig(data_root=data_root, venues=(venue,), pair=symbol, start_date=day, end_date=day, latency_preset="colocated_both",
                            bybit_fee_preset="bybit_perp_vip0", binance_fee_preset="binance_perp_vip0", maker=MakerConfig(**maker_kw),
                            arb=ArbConfig(mode="maker_taker", maker_max_live_orders=10 ** 6, maker_max_age_s=0.0, max_position_base=1e12,
                                          max_venue_imbalance_base=1e12), max_trade_log=10 ** 9, output_dir=None)
    with warnings.catch_warnings():            # min_queue_ahead / maker_max_age advisories do not apply to injected at-touch probes
        warnings.simplefilter("ignore")
        bt = CrossVenueArbBacktester(cfg)
    bt.strategy.evaluate = lambda *a, **k: None
    bt.strategy.hedge_leg = lambda *a, **k: None
    bt.latency.entry_delay = lambda v: 0.0                 # a real order's creation stamp IS its exchange arrival
    fills = collections.defaultdict(list)
    cause, init_q, extra = {}, {}, {}
    orig_on_fill = bt._on_fill

    def _on_fill(fill, tracker=None):
        cause.setdefault(fill.signal_id, fill.cause)
        return orig_on_fill(fill, tracker)
    bt._on_fill = _on_fill
    state = {"nxt": 0, "seen": 0}
    orig = bt._apply_event

    def apply(ev):
        ex_ts, now = ev.exchange_ts, ev.receive_ts
        al = bt.latency.aligner
        book = bt.books[venue]
        while state["nxt"] < len(orders) and orders[state["nxt"]]["created"] <= ex_ts:
            o = orders[state["nxt"]]
            state["nxt"] += 1
            sid = state["nxt"]
            arrive = al.to_host(o["created"]) if al.ready else o["created"]
            x = {"landing_ready": bool(book.ready)}
            if book.ready:
                x.update(land_bid=book.best_bid, land_ask=book.best_ask,
                         cross_at_landing=bool((o["price"] >= book.best_ask - 1e-12) if o["side"] == "Buy" else (o["price"] <= book.best_bid + 1e-12)))
            extra[sid] = x
            leg = ArbLeg(venue=venue, side="buy" if o["side"] == "Buy" else "sell", qty=o["qty"], limit_price=o["price"], is_taker=False,
                         expected_price=o["price"], fee_bps=0.0)
            bt._fire(ArbSignal(signal_id=sid, ts=arrive, buy_venue=venue, sell_venue=venue, qty=o["qty"], gross_edge_bps=0.0, fee_bps=0.0,
                               net_edge_bps=0.0, legs=[leg], mode="maker_taker", buy_price=o["price"], sell_price=o["price"]), min(arrive, now))
        bt._advance_to(now)
        orig(ev)
        for row in bt.trade_log[state["seen"]:]:
            if not row["taker"]:
                fills[row["signal_id"]].append((ex_ts, row["qty"]))
        state["seen"] = len(bt.trade_log)
        for mo in list(bt.maker.orders.values()):
            sid = bt._maker_to_signal.get(mo.order_id)
            if sid is None:
                continue
            init_q.setdefault(sid, mo.queue_ahead)
            if ex_ts >= orders[sid - 1]["end"] + grace_s:
                bt.maker.cancel(mo.order_id, now)
    bt._apply_event = apply
    bt.run()
    rows = []
    for i, o in enumerate(orders, 1):
        f = fills.get(i, [])
        rows.append(dict(o, sim_filled=sum(q for _, q in f), sim_first=(f[0][0] if f else None), sim_last=(f[-1][0] if f else None),
                         sim_first_cause=cause.get(i), sim_queue_at_land=init_q.get(i), **extra.get(i, {})))
    return rows


# ---- scoring -----------------------------------------------------------------------------------------------------------------
def classify(r: dict) -> str:
    """For real-filled orders: hit | premature | late | unfilled. For real-unfilled: false_fill | true_unfilled."""
    if r["real_filled"] > 1e-12:
        if not r["sim_filled"] > 1e-12:
            return "unfilled"
        dt = r["sim_first"] - r["first_fill"]
        return "hit" if abs(dt) <= TOL_S else ("premature" if dt < -TOL_S else "late")
    return "false_fill" if r["sim_filled"] > 1e-12 else "true_unfilled"


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def score(rows: list[dict]) -> dict:
    c = collections.Counter(classify(r) for r in rows)
    nf = sum(c[k] for k in ("hit", "premature", "late", "unfilled"))
    nu = c["false_fill"] + c["true_unfilled"]
    out = {"n": len(rows), "real_filled_orders": nf, "real_unfilled_orders": nu, "counts": dict(c)}
    for k in ("hit", "premature", "late", "unfilled"):
        out[k] = {"share": c[k] / nf if nf else None, "ci95": wilson(c[k], nf)}
    out["false_fill"] = {"share": c["false_fill"] / nu if nu else None, "ci95": wilson(c["false_fill"], nu)}
    rq = sum(r["real_filled"] for r in rows)
    out["qty_ratio_sim_over_real"] = (sum(min(r["sim_filled"], r["qty"]) for r in rows) / rq) if rq > 0 else None
    return out


def recon_summary(run: Path) -> dict:
    f = run / "recon_checks.jsonl"
    if not f.exists():
        return {}
    rows = [json.loads(x) for x in f.read_text().splitlines() if x.strip()]
    out = {}
    for venue in sorted({r["venue"] for r in rows}):
        v = [r for r in rows if r["venue"] == venue]
        al = [r for r in v if r.get("aligned")]
        out[venue] = {"checks": len(v), "aligned": len(al), "top_n_exact_match": sum(1 for r in al if r.get("match")),
                      "top1_match": sum(1 for r in al if r.get("top1_match"))}
    return out


def _load_nb(src: str) -> dict:
    sys.path.insert(0, src)
    from new_backtest.arb_strategy import ArbConfig, ArbLeg, ArbSignal
    from new_backtest.backtester import ArbBacktestConfig, CrossVenueArbBacktester
    from new_backtest.event_loader import BOOK, snapshot_levels
    from new_backtest.fill_engine import MakerConfig
    return dict(ArbConfig=ArbConfig, ArbLeg=ArbLeg, ArbSignal=ArbSignal, ArbBacktestConfig=ArbBacktestConfig,
                CrossVenueArbBacktester=CrossVenueArbBacktester, BOOK=BOOK, snapshot_levels=snapshot_levels, MakerConfig=MakerConfig)


def main(args) -> int:
    run = Path(args.run)
    if not (run / "capture").is_dir():
        print(f"{run} has no capture/ directory", file=sys.stderr)
        return 1
    names = [c.strip() for c in args.configs.split(",") if c.strip()]
    unknown = [n for n in names if n not in CONFIGS]
    if unknown:
        print(f"unknown config(s) {unknown}; choose from {list(CONFIGS)}", file=sys.stderr)
        return 1
    orders = load_orders(run)
    if not orders:
        print("no usable probe orders in the run", file=sys.stderr)
        return 1
    nb = _load_nb(args.src)
    groups = collections.defaultdict(list)
    for o in orders:
        groups[(o["venue"], o["symbol"], o["day"])].append(o)
    results = {}
    for name in names:
        rows = []
        for (venue, sym, day), os_ in sorted(groups.items()):
            rows += replay_one(nb, str(run / "capture"), venue, sym, day, os_, CONFIGS[name], args.grace_s)
        results[name] = {"maker_kw": CONFIGS[name], "score": score(rows), "rows": rows}
        s = results[name]["score"]
        print(f"{name:28s} n={s['n']:4d} real-filled={s['real_filled_orders']:3d} hit={_p(s['hit']['share'])} premature={_p(s['premature']['share'])} "
              f"late={_p(s['late']['share'])} unfilled={_p(s['unfilled']['share'])} | real-unfilled={s['real_unfilled_orders']:3d} false_fill={_p(s['false_fill']['share'])}")
    doc = {"run": str(run), "tolerance_s": TOL_S, "recon_checks": recon_summary(run), "configs": results,
           "note": "testnet runs are a plumbing/event-ordering check, not fill-model evidence" if "testnet" in run.name else ""}
    out = Path(args.out) if args.out else run / "replay_results.json"
    out.write_text(json.dumps(doc, indent=1, default=str))
    print(f"results: {out}")
    return 0


def _p(x) -> str:
    return "  n/a" if x is None else f"{100 * x:5.1f}%"
