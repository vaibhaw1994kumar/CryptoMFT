import json
from pathlib import Path
import tempfile
import unittest

from crypto_stat_arb.alignment import (
    CrossVenueEligibilityPolicy,
    CrossVenueStreamMerger,
    align_canonical_files,
)
from crypto_stat_arb.config import load_config
from crypto_stat_arb.ledger import Ledger
from crypto_stat_arb.models import MarketEvent, PlanState
from crypto_stat_arb.oms import OMS
from crypto_stat_arb.orderbook import OrderBook
from crypto_stat_arb.risk import RiskEngine
from crypto_stat_arb.strategy import CrossVenueSpreadStrategy


ROOT = Path(__file__).parents[1]


def snapshot(venue: str, sequence: int, receive: int, bid: str, ask: str) -> MarketEvent:
    return MarketEvent.from_dict({
        "event_type": "BOOK_SNAPSHOT", "venue": venue,
        "instrument_id": "BTC-USDT-SPOT", "source_ts_ns": receive - 2,
        "exchange_ts_ns": receive - 1, "receive_ts_ns": receive,
        "sequence": sequence, "bids": [[bid, "1"]], "asks": [[ask, "1"]],
        "native": {"venue": venue, "marker": sequence},
    })


class CrossVenueAlignmentTests(unittest.TestCase):
    def test_merge_is_stable_and_preserves_original_events(self):
        binance = snapshot("BINANCE", 1, 20, "99", "100")
        bybit = snapshot("BYBIT", 1, 10, "100", "101")
        before = (dict(binance.native), dict(bybit.native))
        merged = list(CrossVenueStreamMerger().merge({
            "BYBIT": [bybit], "BINANCE": [binance],
        }))
        self.assertEqual(merged, [bybit, binance])
        self.assertIs(merged[0], bybit)
        self.assertIs(merged[1], binance)
        self.assertEqual((binance.native, bybit.native), before)

    def test_eligibility_requires_every_valid_fresh_input(self):
        books = {
            (venue, "BTC-USDT-SPOT"): OrderBook(venue, "BTC-USDT-SPOT")
            for venue in ("BINANCE", "BYBIT")
        }
        policy = CrossVenueEligibilityPolicy(
            ("BINANCE", "BYBIT"), "BTC-USDT-SPOT", max_age_ns=10
        )
        books[("BINANCE", "BTC-USDT-SPOT")].apply(snapshot("BINANCE", 1, 100, "99", "100"))
        self.assertFalse(policy.evaluate(books, 100).eligible)
        books[("BYBIT", "BTC-USDT-SPOT")].apply(snapshot("BYBIT", 1, 100, "101", "102"))
        self.assertTrue(policy.evaluate(books, 100).eligible)
        stale = policy.evaluate(books, 111)
        self.assertFalse(stale.eligible)
        self.assertEqual({item.book_state.value for item in stale.inputs}, {"STALE"})

    def test_oms_rechecks_same_fingerprint_before_routing(self):
        cfg, _ = load_config(ROOT / "config" / "research.json")
        books = {
            ("BINANCE", cfg.instrument_id): OrderBook("BINANCE", cfg.instrument_id),
            ("BYBIT", cfg.instrument_id): OrderBook("BYBIT", cfg.instrument_id),
        }
        books[("BINANCE", cfg.instrument_id)].apply(snapshot("BINANCE", 1, 100, "59999.9", "60000"))
        books[("BYBIT", cfg.instrument_id)].apply(snapshot("BYBIT", 1, 100, "60060", "60060.1"))
        policy = CrossVenueEligibilityPolicy(cfg.venues, cfg.instrument_id, cfg.max_book_age_ns)
        strategy = CrossVenueSpreadStrategy(
            cfg.strategy_id, cfg.instrument_id, cfg.venues, cfg.quantity,
            cfg.entry_bps, cfg.max_book_age_ns, cfg.min_net_edge_bps,
            cfg.instruments, policy,
        )
        opportunity = strategy.evaluate(books, 100)
        self.assertIsNotNone(opportunity)
        books[("BINANCE", cfg.instrument_id)].apply(MarketEvent.from_dict({
            "event_type": "BOOK_DELTA", "venue": "BINANCE",
            "instrument_id": cfg.instrument_id, "exchange_ts_ns": 101,
            "receive_ts_ns": 101, "sequence": 2,
            "bids": [["59999.9", "1"]], "asks": [["60000", "1"]],
        }))
        ledger = Ledger()
        oms = OMS(
            cfg.instruments, RiskEngine(cfg.risk_limits), ledger,
            eligibility_policy=policy,
        )
        plan = oms.execute(opportunity, books)
        self.assertEqual(plan.state, PlanState.CLOSED)
        self.assertEqual(plan.terminal_outcome, PlanState.REJECTED.value)
        self.assertEqual(oms.alignment_rejections, 1)
        self.assertFalse(any(row["event_type"] == "FILL" for row in ledger.events))

    def test_alignment_pipeline_is_reproducible(self):
        rows = [
            json.loads(line)
            for line in (ROOT / "data" / "samples" / "two_venue_books.jsonl").read_text().splitlines()
        ]
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            files = {}
            for venue in ("BINANCE", "BYBIT"):
                path = base / f"{venue.lower()}.jsonl"
                path.write_text("".join(json.dumps(row) + "\n" for row in rows if row["venue"] == venue))
                files[venue] = path
            first = align_canonical_files(ROOT / "config" / "research.json", files, base / "first.jsonl")
            second = align_canonical_files(ROOT / "config" / "research.json", dict(reversed(list(files.items()))), base / "second.jsonl")
            self.assertEqual((base / "first.jsonl").read_bytes(), (base / "second.jsonl").read_bytes())
            self.assertEqual(
                (base / "first.jsonl.eligibility.jsonl").read_bytes(),
                (base / "second.jsonl.eligibility.jsonl").read_bytes(),
            )
            self.assertEqual(first["deterministic_alignment_sha256"], second["deterministic_alignment_sha256"])
            self.assertEqual(first["output_count"], 4)
            self.assertGreater(first["eligible_evaluations"], 0)


if __name__ == "__main__":
    unittest.main()
