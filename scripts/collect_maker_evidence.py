"""Regenerate the evidence behind the maker execution-realism verification checks.

Writes one folder per check under ``reports/evidence/<stamp>/`` plus
``INDEX.md`` and ``evidence_summary.json`` (with SHA-256 of every file):

1. engine_parity          C++ vs Python replay of cycle-0006 (reports + fills)
2. cpp_semantics          C++ engine queue-semantics scenarios
3. live_replay_parity     live paper session vs replay of its own recording
4_5. replay_order_and_ids trace coverage, ordering decision, duplicate ids (all cycles)
6. session_audit          analyze-session on cycle-0006 (new) vs the stored audit (old)
7. cli_simulate_maker     ``crypto-stat-arb simulate-maker`` on cycle-0001
8. sensitivity_sweep      copy of the sweep results with per-run sanity totals

    python3.12 scripts/collect_maker_evidence.py
"""

from __future__ import annotations

import asyncio
import csv
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from crypto_stat_arb.config import load_config  # noqa: E402
from crypto_stat_arb.maker_engine import load_cpp_module  # noqa: E402
from crypto_stat_arb.maker_sim import (  # noqa: E402
    _trace_ordinals,
    maker_simulator_from_config,
    simulate_recorded_session,
)
from crypto_stat_arb.session_audit import audit_session  # noqa: E402

CONFIG = ROOT / "config" / "paper_watch.json"
LIVE = ROOT / "runs" / "paper_watch" / "live"
CYCLE6 = LIVE / "cycle-0006-20260914T142731Z"
CYCLE1 = LIVE / "cycle-0001-20260913T170323Z"
SWEEP = ROOT / "runs" / "maker_sensitivity" / "20261011T172155Z"


def digest(path: Path) -> str:
    h = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def parts(cycle: Path) -> list[Path]:
    return sorted(cycle.glob("part-*"))


def write(path: Path, document) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True, default=str), encoding="utf-8")
    return path


def venue_brief(report: dict) -> dict:
    keys = ("quotes", "quotes_kept", "fills", "fills_trade_through", "orders_filled",
            "post_only_rejects", "cancels_sent", "cancels_applied", "fills_while_cancel_pending",
            "mean_queue_ahead_at_arrival", "crossed_without_print", "inventory",
            "mark_to_market_pnl_quote", "fees", "sanity_violation_count")
    return {venue: {k: row.get(k) for k in keys} for venue, row in report["venues"].items()}


# 1 -----------------------------------------------------------------------------
def engine_parity(out: Path) -> dict:
    result = {"cycle": CYCLE6.name}
    for engine in ("cpp", "python"):
        started = time.time()
        report = simulate_recorded_session(
            CONFIG, [p / "canonical.jsonl" for p in parts(CYCLE6)],
            [p / "trades.jsonl" for p in parts(CYCLE6)], out / f"maker_{engine}.json", engine=engine,
        )
        fills = out / f"maker_{engine}_fills.jsonl"
        result[engine] = {
            "engine": report["engine"], "runtime_s": round(time.time() - started, 1),
            "replay": report["replay"], "fill_rows": sum(1 for _ in fills.open()),
            "fills_sha256": digest(fills), "venues": venue_brief(report),
        }
    result["fills_byte_identical"] = result["cpp"]["fills_sha256"] == result["python"]["fills_sha256"]
    result["decision"] = "PASS" if result["fills_byte_identical"] else "FAIL"
    write(out / "parity_summary.json", result)
    return result


# 2 -----------------------------------------------------------------------------
def cpp_semantics(out: Path) -> dict:
    m = load_cpp_module()
    if m is None:
        return write(out / "scenarios.json", {"decision": "SKIPPED", "reason": "C++ module not importable"}) and {"decision": "SKIPPED"}
    rows = []

    def engine():
        return m.MatchingEngine(m.FeeConfig(0.0, 0.0))

    for order in (("trade", "book"), ("book", "trade")):
        e = engine()
        e.update_bid_level(100.0, 10.0)
        oid = e.place_order(m.Side.BID, 100.0, 2.0)
        e.on_trade(100.0, 4.0, m.AggressorSide.SELL)
        fills = []
        for step in order:
            if step == "book":
                e.update_bid_level(100.0, 3.0)
            else:
                fills += e.on_trade(100.0, 3.0, m.AggressorSide.SELL)
        rows.append({"scenario": f"print + its book delta, {order[0]} first",
                     "setup": "level 10, our bid 2; print 4; then print 3 and level 10->3",
                     "expected": {"queue_ahead": 3.0, "fills": []},
                     "observed": {"queue_ahead": e.queue_ahead_remaining(oid),
                                  "fills": [f.filled_qty for f in fills]}})
    e = engine()
    e.update_bid_level(100.0, 5.0)
    a = e.place_order(m.Side.BID, 100.0, 1.0)
    b = e.place_order(m.Side.BID, 100.0, 1.0)
    fills = e.on_trade(100.0, 6.5, m.AggressorSide.SELL)
    rows.append({"scenario": "two own orders share one print (FIFO)",
                 "setup": "level 5, orders a=1 then b=1, print 6.5",
                 "expected": {"fills": [[a, 1.0], [b, 0.5]]},
                 "observed": {"fills": [[f.order_id, f.filled_qty] for f in fills]}})
    e.update_bid_level(100.0, 0.0)
    rows.append({"scenario": "level updated to size 0 keeps our resting order",
                 "setup": "after previous scenario, level 100 -> 0",
                 "expected": {"order_b_active": True, "level_100_our_orders": 1},
                 "observed": {"order_b_active": e.get_order(b).active,
                              "level_100_our_orders": next(l.our_orders for l in e.bid_levels() if l.price == 100.0)}})
    fills = e.on_trade(99.0, 0.1, m.AggressorSide.SELL)
    rows.append({"scenario": "print through our price fills the remainder",
                 "setup": "order b remaining 0.5 at 100, SELL print 0.1 at 99",
                 "expected": {"fills": [[b, 0.5]]},
                 "observed": {"fills": [[f.order_id, f.filled_qty] for f in fills]}})
    e = engine()
    e.set_queue_position_frac(0.7)
    e.update_bid_level(100.0, 10.0)
    o = e.place_order(m.Side.BID, 100.0, 1.0)
    rows.append({"scenario": "queue_position_frac = 0.7",
                 "setup": "level 10, place bid", "expected": {"queue_ahead": 7.0},
                 "observed": {"queue_ahead": e.queue_ahead_remaining(o)}})
    for row in rows:
        row["pass"] = row["expected"] == row["observed"]
    result = {"module": m.__file__, "scenarios": rows,
              "decision": "PASS" if all(r["pass"] for r in rows) else "FAIL"}
    write(out / "scenarios.json", result)
    return result


# 3 -----------------------------------------------------------------------------
def live_replay_parity(out: Path) -> dict:
    from crypto_stat_arb.live_feed import LiveFeedSettings
    from crypto_stat_arb.paper_live import LivePaperSession
    from test_live_feed import FakeWebSocket, StepClock
    from test_maker_execution_path import BybitTopicSocket, RoutingConnector

    bybit_books = [json.dumps({
        "topic": "orderbook.50.BTCUSDT", "type": "snapshot", "ts": 1000,
        "data": {"s": "BTCUSDT", "u": 1, "b": [["60000.00", "1.0"]], "a": [["60000.10", "1.0"]]},
    })] + [json.dumps({
        "topic": "orderbook.50.BTCUSDT", "type": "delta", "ts": 1000 + n,
        "data": {"s": "BTCUSDT", "u": 1 + n, "b": [["60000.00", f"{1 + n % 3}.0"]], "a": []},
    }) for n in range(1, 60)]
    bybit_trades = [json.dumps({"topic": "publicTrade.BTCUSDT", "data": [{
        "s": "BTCUSDT", "i": f"t{n}", "T": 1000 + n, "p": "60000.00" if n % 2 else "60000.10",
        "v": "1.5", "S": "Sell" if n % 2 else "Buy", "seq": n,
    }]}) for n in range(1, 50)]
    binance_books = [json.dumps({
        "e": "depthUpdate", "E": 1000 + n, "T": 1000 + n, "s": "BTCUSDT",
        "U": 101 + n, "u": 101 + n, "b": [["59999.90", f"{1 + n % 2}"]], "a": [],
    }) for n in range(40)]
    binance_trades = [json.dumps({
        "e": "trade", "E": 1000 + n, "T": 1000 + n, "s": "BTCUSDT", "t": n,
        "p": "59999.90" if n % 2 else "60000.00", "q": "2", "m": bool(n % 2),
    }) for n in range(1, 40)]
    clock = StepClock()

    async def http_getter(url, timeout):
        await asyncio.sleep(0)
        return json.dumps({"lastUpdateId": 100, "bids": [["59999.90", "1"]],
                           "asks": [["60000.00", "1"]]}), clock()

    cfg, _ = load_config(CONFIG)
    maker = maker_simulator_from_config(cfg)
    session_root = out / "session"
    if session_root.exists():
        shutil.rmtree(session_root)
    paper = LivePaperSession(
        CONFIG, session_root, "parity",
        LiveFeedSettings(max_events=90, reconnect_min_seconds=0.001, reconnect_max_seconds=0.002,
                         durable=False, udp_enabled=False, trade_streams_enabled=True),
        websocket_connector=RoutingConnector(
            FakeWebSocket(binance_books), FakeWebSocket(binance_trades),
            [BybitTopicSocket(bybit_books, []), BybitTopicSocket([], bybit_trades)],
        ),
        http_getter=http_getter, clock_ns=clock, tick_seconds=10, maker_simulator=maker,
    )
    summary = asyncio.run(asyncio.wait_for(paper.run(), timeout=20))
    session_dir = session_root / "parity"
    live_fills = out / "live_fills.jsonl"
    live_fills.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in maker.fills))
    replay = simulate_recorded_session(
        CONFIG, [p / "canonical.jsonl" for p in parts(session_dir)],
        [p / "trades.jsonl" for p in parts(session_dir)], out / "replay.json",
    )
    replay_fills = out / "replay_fills.jsonl"
    result = {
        "recorded": {
            "book_events": sum(1 for p in parts(session_dir) for _ in (p / "canonical.jsonl").open()),
            "trades": sum(1 for p in parts(session_dir) for _ in (p / "trades.jsonl").open()),
        },
        "replay_ordering": replay["replay"]["ordering"],
        "live": {"fill_rows": len(maker.fills), "fills_sha256": digest(live_fills),
                 "venues": venue_brief(summary["maker_dry_run"])},
        "replay": {"fill_rows": sum(1 for _ in replay_fills.open()), "fills_sha256": digest(replay_fills),
                   "venues": venue_brief(replay)},
    }
    result["identical"] = result["live"]["fills_sha256"] == result["replay"]["fills_sha256"]
    result["decision"] = "PASS" if result["identical"] and result["live"]["fill_rows"] > 0 else "FAIL"
    write(out / "parity_summary.json", result)
    return result


# 4 + 5 -------------------------------------------------------------------------
def replay_order_and_ids(out: Path) -> dict:
    rows = []
    for cycle in sorted(LIVE.glob("cycle-*")):
        ps = parts(cycle)
        book_ids, trade_ids, trace_ids = [], [], []
        for p in ps:
            book_ids += [json.loads(l)["event_id"] for l in (p / "canonical.jsonl").open() if l.strip()]
            if (p / "trades.jsonl").exists():
                trade_ids += [json.loads(l)["event_id"] for l in (p / "trades.jsonl").open() if l.strip()]
            if (p / "trace.jsonl").exists():
                trace_ids += [r["canonical_event_id"] for r in map(json.loads, (p / "trace.jsonl").open())
                              if r.get("disposition") in ("CANONICAL", "TRADE")]
        ordinals = _trace_ordinals([p / "trace.jsonl" for p in ps])
        events = book_ids + trade_ids
        covered = sum(1 for e in events if e in ordinals)
        dup_trace = len(trace_ids) - len(set(trace_ids))
        dup_events = len(events) - len(set(events))
        ordering = "arrival_ordinal" if events and covered == len(events) and not dup_events else "receive_ts+tie_priority"
        rows.append({"cycle": cycle.name, "parts": len(ps), "book_events": len(book_ids),
                     "trades": len(trade_ids), "trace_rows": len(trace_ids),
                     "covered_by_trace": covered, "duplicate_trace_ids": dup_trace,
                     "duplicate_event_ids": dup_events, "ordering": ordering})
    with (out / "cycles.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    result = {
        "cycles": len(rows),
        "arrival_ordinal_cycles": sum(r["ordering"] == "arrival_ordinal" for r in rows),
        "duplicate_trace_ids_total": sum(r["duplicate_trace_ids"] for r in rows),
        "duplicate_event_ids_total": sum(r["duplicate_event_ids"] for r in rows),
        "events_total": sum(r["book_events"] + r["trades"] for r in rows),
    }
    result["decision"] = "PASS" if result["duplicate_trace_ids_total"] == 0 else "FAIL"
    write(out / "summary.json", result)
    return result


# 6 -----------------------------------------------------------------------------
def session_audit(out: Path) -> dict:
    audit = audit_session(CONFIG, CYCLE6, out / "new")
    old = json.loads((ROOT / "runs/paper_watch/audits" / CYCLE6.name / "session_audit.json").read_text())
    shutil.copy(ROOT / "runs/paper_watch/audits" / CYCLE6.name / "session_audit.json", out / "old_session_audit.json")

    def sim(component):
        return {"status": component["status"],
                "checks": [{k: c.get(k) for k in ("check", "status", "detail")} for c in component["checks"]],
                "metrics": component.get("metrics")}

    result = {
        "cycle": CYCLE6.name,
        "old": {"simulation": sim(old["components"]["simulation"]),
                "components": {k: v["status"] for k, v in old["components"].items()}},
        "new": {"simulation": sim(audit["components"]["simulation"]),
                "components": {k: v["status"] for k, v in audit["components"].items()}},
    }
    result["decision"] = "PASS" if result["new"]["simulation"]["status"] == "OK" else "FAIL"
    write(out / "comparison.json", result)
    return result


# 7 -----------------------------------------------------------------------------
def cli_simulate_maker(out: Path) -> dict:
    command = [sys.executable, "-m", "crypto_stat_arb.cli", "simulate-maker", "--config", str(CONFIG),
               "--session-dir", str(CYCLE1), "--output", str(out / "cli_maker.json")]
    run = subprocess.run(command, capture_output=True, text=True, cwd=ROOT,
                         env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"})
    (out / "stdout.json").write_text(run.stdout)
    report = json.loads(run.stdout)
    result = {"command": " ".join(command[1:]), "exit_code": run.returncode, "engine": report["engine"],
              "replay": report["replay"], "latency_source": report["latency"]["source"],
              "fill_rows": report["fill_records"], "venues": venue_brief(report)}
    sanity = sum(v["sanity_violation_count"] for v in report["venues"].values())
    rejected = sum(report["replay"]["rejected_invalid_envelopes"].values())
    result["decision"] = "PASS" if run.returncode == 0 and report["engine"].startswith("cpp") and not sanity and not rejected else "FAIL"
    write(out / "summary.json", result)
    return result


# 8 -----------------------------------------------------------------------------
def sensitivity_sweep(out: Path) -> dict:
    for name in ("results.csv", "summary.json", "SENSITIVITY.md"):
        shutil.copy(SWEEP / name, out / name)
    rows = list(csv.DictReader((SWEEP / "results.csv").open()))
    result = {
        "source": str(SWEEP.relative_to(ROOT)), "runs": len({(r["cycle"], r["feed_lag_ms"], r["queue_position_frac"]) for r in rows}),
        "rows": len(rows), "cycles": sorted({r["cycle"] for r in rows}),
        "engine": sorted({r["engine"] for r in rows}), "ordering": sorted({r["ordering"] for r in rows}),
        "sanity_violations_total": sum(int(r["sanity_violations"]) for r in rows),
        "max_sanity_violations_in_any_run": max(int(r["sanity_violations"]) for r in rows),
    }
    result["decision"] = "PASS" if result["sanity_violations_total"] == 0 else "FAIL"
    write(out / "sanity_summary.json", result)
    return result


CHECKS = [
    ("1_engine_parity", "C++ vs Python engine parity", engine_parity),
    ("2_cpp_semantics", "C++ queue semantics smoke test", cpp_semantics),
    ("3_live_replay_parity", "Live paper vs replay parity", live_replay_parity),
    ("4_5_replay_order_and_ids", "Replay ordering + duplicate event ids (all cycles)", replay_order_and_ids),
    ("6_session_audit", "Session audit analyze-session (cycle-0006)", session_audit),
    ("7_cli_simulate_maker", "CLI simulate-maker (cycle-0001)", cli_simulate_maker),
    ("8_sensitivity_sweep", "Sensitivity sweep sanity", sensitivity_sweep),
]


def main() -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = ROOT / "reports" / "evidence" / stamp
    summary = {"generated": stamp, "checks": {}}
    for folder, title, func in CHECKS:
        out = root / folder
        out.mkdir(parents=True, exist_ok=True)
        started = time.time()
        print(f"[{folder}] {title} ...", flush=True)
        result = func(out)
        summary["checks"][folder] = {"title": title, "decision": result.get("decision"),
                                     "seconds": round(time.time() - started, 1), "result": result}
        print(f"[{folder}] {result.get('decision')} ({time.time() - started:.0f}s)", flush=True)
    files = {str(p.relative_to(root)): {"bytes": p.stat().st_size, "sha256": digest(p)}
             for p in sorted(root.rglob("*")) if p.is_file()}
    summary["files"] = files
    write(root / "evidence_summary.json", summary)
    lines = [f"# Maker execution-realism evidence ({stamp})", "",
             "| Check | Decision | Folder |", "|---|---|---|"]
    lines += [f"| {c['title']} | {c['decision']} | `{k}/` |" for k, c in summary["checks"].items()]
    lines += ["", f"{len(files)} files; SHA-256 of each in `evidence_summary.json`."]
    (root / "INDEX.md").write_text("\n".join(lines) + "\n")
    print(root)


if __name__ == "__main__":
    main()
