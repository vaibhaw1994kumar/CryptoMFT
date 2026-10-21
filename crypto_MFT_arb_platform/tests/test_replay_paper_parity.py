"""Replay must keep the book in sync when a time-integrity check blocks an event.

Live, the feed service applies every sequenced delta to the book before the
session sees it; a timestamp anomaly only blocks decisions. A replay that also
dropped the delta opened a sequence gap and kept the book invalid until the
next venue snapshot (minutes on Bybit), which made replay-vs-paper diverge
(cycle-0010: 25 replay vs 44 paper opportunities).
"""

from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from crypto_stat_arb.calibration import compare_runs

from crypto_stat_arb.config import load_config
from crypto_stat_arb.models import BookState, EventType, Level, MarketEvent
from crypto_stat_arb.session import TradingSession


ROOT = Path(__file__).parents[1]
CONFIG = ROOT / "config" / "research.json"
INSTRUMENT = "BTC-USDT-SPOT"
T0 = 1_700_000_000_000_000_000
MS = 1_000_000


def event(venue, sequence, exchange_ts, receive_ts, bids, asks, snapshot=False):
    return MarketEvent(
        event_id=f"{venue}-{sequence}",
        event_type=EventType.BOOK_SNAPSHOT if snapshot else EventType.BOOK_DELTA,
        venue=venue, instrument_id=INSTRUMENT,
        exchange_ts_ns=exchange_ts, receive_ts_ns=receive_ts, source_ts_ns=exchange_ts,
        sequence=sequence, sequence_start=sequence, raw_ref=f"raw-{sequence}",
        adapter_version="test",
        bids=tuple(Level(Decimal(p), Decimal(q)) for p, q in bids),
        asks=tuple(Level(Decimal(p), Decimal(q)) for p, q in asks),
    )


class BlockedEventBookTests(unittest.TestCase):
    def test_blocked_delta_is_applied_to_the_book_but_not_decided_on(self):
        cfg, _ = load_config(CONFIG)
        session = TradingSession(cfg)
        evaluated = []
        strategy = session.strategies[0]
        original = strategy.evaluate

        def counting(books, now):
            evaluated.append(now)
            return original(books, now)

        strategy.evaluate = counting
        for venue in ("BINANCE", "BYBIT"):
            session.on_event(event(venue, 1, T0, T0 + MS, [("60000", "1")], [("60000.1", "1")], snapshot=True))
        calls_before = len(evaluated)
        # Receive time before exchange time: NEGATIVE_RECEIVE_LATENCY blocks it.
        blocked = event("BINANCE", 2, T0 + 10 * MS, T0 + 5 * MS, [("60000", "3")], [])
        session.on_event(blocked)
        self.assertEqual(len(evaluated), calls_before, "strategy must not run on a blocked event")
        self.assertTrue(any(
            row["event_type"] == "TIME_INTEGRITY_BLOCK" for row in session.ledger.events
        ))
        session.on_event(event("BINANCE", 3, T0 + 20 * MS, T0 + 21 * MS, [], [("60000.1", "2")]))
        book = session.books[("BINANCE", INSTRUMENT)]
        self.assertEqual(book.state, BookState.VALID)
        self.assertEqual(book.sequence, 3)
        self.assertEqual(book.bids[Decimal("60000")], Decimal("3"))  # the blocked delta's update
        self.assertEqual(book.stats.gaps, 0)



def write_run(run, mode, *, net_pnl, equity, latency_p50=1_000_000):
    run.mkdir(parents=True)
    (run / "summary.json").write_text(json.dumps({
        "opportunities": 50, "closed_plans": 50, "net_pnl": str(net_pnl),
        "fees": "5", "execution_mode": mode,
    }))
    (run / "tca_report.json").write_text(json.dumps({"summary": {
        "orders": 100, "fills": 80, "rejects": 10, "cancels": 0,
        "missed_opportunities": 10, "weighted_slippage_bps": "0", "fees": "5",
    }}))
    (run / "account_state.json").write_text(json.dumps({
        "venues": {"BINANCE": {"equity": str(10_000 + equity), "initial_capital": "10000"}},
        "positions": {"BINANCE:X": {"quantity": "-0.001"}},
    }))
    (run / "latency_report.json").write_text(json.dumps({"model": {"stages": {
        "BINANCE.strategy": {"p50_ns": latency_p50, "p99_ns": latency_p50},
    }}}))


class CompareRunsTests(unittest.TestCase):
    def compare(self, paper, replay):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            write_run(base / "paper", "INTERNAL_PAPER", **paper)
            write_run(base / "replay", "INTERNAL_SIMULATION", **replay)
            return compare_runs(base / "replay", base / "paper")

    def test_unhedged_leg_cash_does_not_gate_when_marked_pnl_agrees(self):
        report = self.compare({"net_pnl": 231.7, "equity": -3.65}, {"net_pnl": -3.8, "equity": -3.83})
        self.assertEqual(report["decision"], "CONSISTENT")
        self.assertFalse(report["informational_metrics"]["net_pnl"]["gating"])
        self.assertAlmostEqual(report["metrics"]["equity_pnl"]["paper"], -3.65)

    def test_marked_pnl_gap_gates(self):
        report = self.compare({"net_pnl": 0, "equity": -3.6}, {"net_pnl": 0, "equity": -20.0})
        self.assertEqual(report["trading_decision"], "DIVERGED")
        self.assertIn("equity_pnl", report["diverged_metrics"])

    def test_latency_only_divergence_is_reported_separately(self):
        report = self.compare(
            {"net_pnl": 1, "equity": -3.6, "latency_p50": 3_000_000},
            {"net_pnl": 1, "equity": -3.6, "latency_p50": 200_000},
        )
        self.assertEqual(
            (report["decision"], report["trading_decision"], report["latency_decision"]),
            ("DIVERGED", "CONSISTENT", "DIVERGED"),
        )


if __name__ == "__main__":
    unittest.main()
