import json
from pathlib import Path
import tempfile
import unittest

from crypto_stat_arb.book_gate import BookIntegrityGate
from crypto_stat_arb.models import BookState, MarketEvent
from crypto_stat_arb.orderbook import OrderBook
from crypto_stat_arb.runner import run_backtest


ROOT = Path(__file__).parents[1]


def event(venue: str, sequence: int, event_type: str, receive_ts_ns: int) -> MarketEvent:
    return MarketEvent.from_dict({
        "event_type": event_type,
        "venue": venue,
        "instrument_id": "BTC-USDT-SPOT",
        "exchange_ts_ns": receive_ts_ns,
        "receive_ts_ns": receive_ts_ns,
        "sequence": sequence,
        "bids": [["100", "2"]],
        "asks": [["101", "2"]],
    })


class BookIntegrityGateTests(unittest.TestCase):
    def setUp(self):
        self.binance = OrderBook("BINANCE", "BTC-USDT-SPOT")
        self.bybit = OrderBook("BYBIT", "BTC-USDT-SPOT")
        self.cancel_calls = []
        self.gate = BookIntegrityGate(
            max_book_age_ns=100,
            cancel_callback=lambda reason, now: self.cancel_calls.append((reason, now)) or 2,
        )

    def test_gap_suspends_cancels_and_fresh_snapshot_resumes(self):
        self.binance.apply(event("BINANCE", 10, "BOOK_SNAPSHOT", 10))
        self.bybit.apply(event("BYBIT", 20, "BOOK_SNAPSHOT", 10))
        self.assertTrue(self.gate.allow((self.binance, self.bybit), 10))

        self.assertEqual(
            self.binance.apply(event("BINANCE", 12, "BOOK_DELTA", 11)),
            BookState.GAP_DETECTED,
        )
        self.assertFalse(self.gate.allow((self.binance, self.bybit), 11))
        self.assertEqual(len(self.cancel_calls), 1)
        self.assertEqual(self.gate.cancelled_orders, 2)

        self.binance.apply(event("BINANCE", 12, "BOOK_SNAPSHOT", 12))
        self.assertTrue(self.gate.allow((self.binance, self.bybit), 12))
        self.assertFalse(self.gate.suspended)

    def test_stale_book_suspends_once_without_cancel_storm(self):
        self.binance.apply(event("BINANCE", 10, "BOOK_SNAPSHOT", 10))
        self.bybit.apply(event("BYBIT", 20, "BOOK_SNAPSHOT", 10))
        self.gate.allow((self.binance, self.bybit), 10)
        self.assertFalse(self.gate.allow((self.binance, self.bybit), 111))
        self.assertFalse(self.gate.allow((self.binance, self.bybit), 112))
        self.assertEqual(len(self.cancel_calls), 1)
        self.assertEqual(self.binance.state, BookState.STALE)

    def test_feed_stall_sets_venue_risk_reducing_only_and_recovers(self):
        transitions = []

        def venue_control(venue, valid, reason, now_ns):
            transitions.append((venue, valid, reason, now_ns))
            return 3 if not valid else 0

        gate = BookIntegrityGate(
            max_book_age_ns=100,
            cancel_callback=lambda _reason, _now: 99,
            venue_control_callback=venue_control,
        )
        self.binance.apply(event("BINANCE", 10, "BOOK_SNAPSHOT", 10))
        self.bybit.apply(event("BYBIT", 20, "BOOK_SNAPSHOT", 20))
        self.assertTrue(gate.allow((self.binance, self.bybit), 20))

        self.assertFalse(gate.allow((self.binance, self.bybit), 111))
        self.assertEqual(gate.venue_state("BINANCE"), "RISK_REDUCING_ONLY")
        self.assertEqual(gate.venue_state("BYBIT"), "ACTIVE")
        self.assertEqual(gate.cancel_requests, 1)
        self.assertEqual(gate.cancelled_orders, 3)

        self.binance.apply(event("BINANCE", 11, "BOOK_SNAPSHOT", 112))
        self.assertTrue(gate.allow((self.binance, self.bybit), 112))
        self.assertEqual(gate.venue_state("BINANCE"), "ACTIVE")
        self.assertIn(("BINANCE", False, "STALE:market_data_stale", 111), transitions)
        self.assertIn(("BINANCE", True, "", 112), transitions)

    def test_controlled_gap_blocks_backtest_strategy(self):
        rows = [
            {"event_type": "BOOK_SNAPSHOT", "venue": "BINANCE", "instrument_id": "BTC-USDT-SPOT", "exchange_ts_ns": 1, "receive_ts_ns": 1, "sequence": 10, "bids": [["99", "1"]], "asks": [["100", "1"]]},
            {"event_type": "BOOK_SNAPSHOT", "venue": "BYBIT", "instrument_id": "BTC-USDT-SPOT", "exchange_ts_ns": 2, "receive_ts_ns": 2, "sequence": 20, "bids": [["99", "1"]], "asks": [["100", "1"]]},
            {"event_type": "BOOK_DELTA", "venue": "BINANCE", "instrument_id": "BTC-USDT-SPOT", "exchange_ts_ns": 3, "receive_ts_ns": 3, "sequence": 12, "bids": [["110", "1"]], "asks": []},
            {"event_type": "BOOK_DELTA", "venue": "BYBIT", "instrument_id": "BTC-USDT-SPOT", "exchange_ts_ns": 4, "receive_ts_ns": 4, "sequence": 21, "bids": [["120", "1"]], "asks": []},
        ]
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            events_path = base / "controlled_gap.jsonl"
            events_path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            summary = run_backtest(ROOT / "config" / "research.json", events_path, base / "run")
        self.assertEqual(summary["opportunities"], 0)
        self.assertEqual(summary["data_quality"]["gaps"], 1)
        self.assertEqual(summary["book_integrity_gate"]["cancel_requests"], 2)
        self.assertEqual(
            summary["book_integrity_gate"]["venues"],
            {"BINANCE": "RISK_REDUCING_ONLY", "BYBIT": "RISK_REDUCING_ONLY"},
        )


NS = 1_000_000_000
T0 = 1_709_251_200 * NS          # 2024-03-01 00:00:00 UTC


class GateSuspendedTimeTests(unittest.TestCase):
    """Phase D (owner 8 Nov): the gate reports how long it blocked, per UTC day and cause. Observation only."""

    def setUp(self):
        self.binance = OrderBook("BINANCE", "BTC-USDT-SPOT")
        self.bybit = OrderBook("BYBIT", "BTC-USDT-SPOT")
        self.gate = BookIntegrityGate(max_book_age_ns=10 * NS, cancel_callback=lambda reason, now: 0)

    def test_a_suspension_across_midnight_is_split_by_day_and_named_by_its_cause(self):
        self.binance.apply(event("BINANCE", 10, "BOOK_SNAPSHOT", T0 + 86_390 * NS))
        self.bybit.apply(event("BYBIT", 20, "BOOK_SNAPSHOT", T0 + 86_390 * NS))
        self.assertTrue(self.gate.allow((self.binance, self.bybit), T0 + 86_390 * NS))
        self.binance.apply(event("BINANCE", 12, "BOOK_DELTA", T0 + 86_395 * NS))               # a hole: GAP_DETECTED
        self.assertFalse(self.gate.allow((self.binance, self.bybit), T0 + 86_395 * NS))        # blocked from here
        self.assertEqual(self.gate.suspended_seconds_by_day(T0 + 86_400 * NS + 20 * NS), {"2024-03-01": {"BINANCE:GAP_DETECTED": 5.0}, "2024-03-02": {"BINANCE:GAP_DETECTED": 20.0}})   # an open interval counts
        self.binance.apply(event("BINANCE", 13, "BOOK_SNAPSHOT", T0 + 86_400 * NS + 30 * NS))
        self.bybit.apply(event("BYBIT", 21, "BOOK_SNAPSHOT", T0 + 86_400 * NS + 30 * NS))      # both books fresh again (the age limit is 10 s)
        self.assertTrue(self.gate.allow((self.binance, self.bybit), T0 + 86_400 * NS + 30 * NS))
        self.assertEqual(self.gate.suspended_seconds_by_day(T0 + 99 * 86_400 * NS), {"2024-03-01": {"BINANCE:GAP_DETECTED": 5.0}, "2024-03-02": {"BINANCE:GAP_DETECTED": 30.0}})

    def test_the_start_up_wait_is_counted_too_and_a_cause_change_closes_the_interval(self):
        self.assertFalse(self.gate.allow((self.binance, self.bybit), T0))                      # nothing synced yet
        self.binance.apply(event("BINANCE", 10, "BOOK_SNAPSHOT", T0 + 2 * NS))
        self.assertFalse(self.gate.allow((self.binance, self.bybit), T0 + 2 * NS))             # BYBIT still waits: the cause changes
        self.bybit.apply(event("BYBIT", 20, "BOOK_SNAPSHOT", T0 + 3 * NS))
        self.assertTrue(self.gate.allow((self.binance, self.bybit), T0 + 3 * NS))
        self.assertEqual(self.gate.suspended_seconds_by_day(T0 + 10 * NS), {"2024-03-01": {"BINANCE:SYNCING+BYBIT:SYNCING": 2.0, "BYBIT:SYNCING": 1.0}})


class AnyMessageAgeTests(unittest.TestCase):
    """Owner 8 Nov (Q-G1): the 500 ms rule is unchanged, but a book's age runs from the venue's last message of ANY kind: a quiet best-bid/ask feed with prints is alive, not stale."""

    def _gate_and_books(self):
        gate = BookIntegrityGate(max_book_age_ns=500_000_000, cancel_callback=lambda reason, now: 0)
        a, b = OrderBook("BINANCE", "BTC-USDT-SPOT"), OrderBook("BYBIT", "BTC-USDT-SPOT")
        a.apply(event("BINANCE", 10, "BOOK_SNAPSHOT", T0)); b.apply(event("BYBIT", 20, "BOOK_SNAPSHOT", T0))
        return gate, a, b

    def test_a_silent_book_is_stale_after_500_ms(self):
        gate, a, b = self._gate_and_books()
        self.assertFalse(gate.allow((a, b), T0 + 600_000_000))

    def test_a_print_on_the_venue_keeps_its_book_fresh(self):
        gate, a, b = self._gate_and_books()
        a.note_venue_message(T0 + 400_000_000)
        b.note_venue_message(T0 + 450_000_000)
        self.assertTrue(gate.allow((a, b), T0 + 600_000_000))
        a.note_venue_message(T0 + 100)                                   # never moves the time backwards
        self.assertEqual(a.last_receive_ts_ns, T0 + 400_000_000)


if __name__ == "__main__":
    unittest.main()
