import unittest

from crypto_stat_arb.models import BookState, MarketEvent
from crypto_stat_arb.orderbook import OrderBook


def event(sequence: int, event_type: str = "BOOK_DELTA") -> MarketEvent:
    return MarketEvent.from_dict({
        "event_type": event_type,
        "venue": "BINANCE",
        "instrument_id": "BTC-USDT-SPOT",
        "exchange_ts_ns": sequence,
        "receive_ts_ns": sequence,
        "sequence": sequence,
        "bids": [["100.0", "2"]],
        "asks": [["101.0", "2"]],
    })


class OrderBookTests(unittest.TestCase):
    def test_pre_snapshot_delta_is_buffered_and_reconciled(self):
        book = OrderBook("BINANCE", "BTC-USDT-SPOT")
        self.assertEqual(book.apply(event(11)), BookState.SYNCING)
        self.assertEqual(book.stats.buffered_deltas, 1)
        self.assertEqual(book.apply(event(10, "BOOK_SNAPSHOT")), BookState.VALID)
        self.assertEqual(book.sequence, 11)
        self.assertEqual(book.stats.deltas_applied, 1)

    def test_binance_ranged_delta_bridges_expected_sequence(self):
        book = OrderBook("BINANCE", "BTC-USDT-SPOT")
        book.apply(event(10, "BOOK_SNAPSHOT"))
        ranged = MarketEvent.from_dict({
            "event_type": "BOOK_DELTA", "venue": "BINANCE",
            "instrument_id": "BTC-USDT-SPOT", "exchange_ts_ns": 11,
            "receive_ts_ns": 11, "sequence_start": 11, "sequence": 13,
            "bids": [["100", "3"]], "asks": [["101", "2"]],
        })
        self.assertEqual(book.apply(ranged), BookState.VALID)
        self.assertEqual(book.sequence, 13)

    def test_binance_futures_deltas_chain_on_previous_final_id(self):
        # USDⓈ-M: U jumps (not pu + 1); continuity is pu == the last applied u.
        def futures(start, end, previous):
            return MarketEvent.from_dict({
                "event_type": "BOOK_DELTA", "venue": "BINANCE", "instrument_id": "BTC-USDT-SPOT",
                "exchange_ts_ns": end, "receive_ts_ns": end, "sequence_start": start, "sequence": end,
                "bids": [["100", "2"]], "asks": [["101", "2"]],
                "native": {"U": start, "u": end, "pu": previous},
            })
        book = OrderBook("BINANCE", "BTC-USDT-SPOT")
        book.apply(event(100, "BOOK_SNAPSHOT"))
        self.assertEqual(book.apply(futures(95, 110, 90)), BookState.VALID)     # bridges U <= 100 <= u
        self.assertEqual(book.apply(futures(130, 140, 110)), BookState.VALID)   # pu == 110: no gap
        self.assertEqual(book.apply(futures(160, 170, 150)), BookState.GAP_DETECTED)  # missed 140..150
        self.assertEqual(book.stats.gaps, 1)
        book.apply(event(200, "BOOK_SNAPSHOT"))
        self.assertEqual(book.apply(futures(205, 210, 201)), BookState.GAP_DETECTED)  # does not bridge

    def test_sequence_gap_invalidates_book_until_snapshot(self):
        book = OrderBook("BINANCE", "BTC-USDT-SPOT")
        self.assertEqual(book.apply(event(10, "BOOK_SNAPSHOT")), BookState.VALID)
        self.assertEqual(book.apply(event(12)), BookState.GAP_DETECTED)
        self.assertEqual(book.apply(event(13, "BOOK_SNAPSHOT")), BookState.VALID)

    def test_duplicate_is_idempotent(self):
        book = OrderBook("BINANCE", "BTC-USDT-SPOT")
        snapshot = event(10, "BOOK_SNAPSHOT")
        book.apply(snapshot)
        first_hash = book.snapshot_hash()
        book.apply(snapshot)
        self.assertEqual(book.snapshot_hash(), first_hash)
        self.assertEqual(book.stats.duplicates, 1)

    def test_out_of_order_delta_is_counted_and_not_applied(self):
        book = OrderBook("BINANCE", "BTC-USDT-SPOT")
        book.apply(event(10, "BOOK_SNAPSHOT"))
        book.apply(event(11))
        before = book.snapshot_hash()
        book.apply(event(9))
        self.assertEqual(book.snapshot_hash(), before)
        self.assertEqual(book.stats.out_of_order, 1)

    def test_repeated_reconstruction_has_identical_hash(self):
        events = [event(10, "BOOK_SNAPSHOT"), event(11), event(12)]
        hashes = []
        for _ in range(3):
            book = OrderBook("BINANCE", "BTC-USDT-SPOT")
            for update in events:
                book.apply(update)
            hashes.append(book.snapshot_hash())
        self.assertEqual(len(set(hashes)), 1)

    def test_checksum_failure_invalidates_book(self):
        book = OrderBook(
            "BINANCE", "BTC-USDT-SPOT",
            checksum_validator=lambda _book, update: update.checksum == "expected",
        )
        checked = MarketEvent.from_dict({
            "event_type": "BOOK_SNAPSHOT", "venue": "BINANCE",
            "instrument_id": "BTC-USDT-SPOT", "exchange_ts_ns": 1,
            "receive_ts_ns": 1, "sequence": 1, "checksum": "bad",
            "bids": [["100", "1"]], "asks": [["101", "1"]],
        })
        self.assertEqual(book.apply(checked), BookState.CHECKSUM_FAILED)
        self.assertEqual(book.stats.checksum_failures, 1)

    def test_crossed_book_is_flagged_not_repaired(self):
        book = OrderBook("BINANCE", "BTC-USDT-SPOT")
        crossed = MarketEvent.from_dict({
            "event_type": "BOOK_SNAPSHOT",
            "venue": "BINANCE",
            "instrument_id": "BTC-USDT-SPOT",
            "exchange_ts_ns": 1,
            "receive_ts_ns": 1,
            "sequence": 1,
            "bids": [["102", "1"]],
            "asks": [["101", "1"]],
        })
        self.assertEqual(book.apply(crossed), BookState.CHECKSUM_FAILED)
        self.assertEqual(book.best_bid, 102)
        self.assertEqual(book.best_ask, 101)


if __name__ == "__main__":
    unittest.main()
