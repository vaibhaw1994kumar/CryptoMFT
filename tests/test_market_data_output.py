import json
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from crypto_stat_arb.market_data_output import (
    LtpOhlcCsvWriter,
    LtpOhlcResampler,
    OhlcCsvWriter,
    OrderBookOhlcResampler,
    UdpMulticastPublisher,
    UdpPublisherSettings,
    normalize_binance_trade,
    normalize_bybit_trades,
)
from crypto_stat_arb.models import EventType, MarketEvent
from crypto_stat_arb.orderbook import OrderBook


def event(sequence, receive_ts_ns, bids, asks, event_type=EventType.BOOK_DELTA):
    return MarketEvent.from_dict({
        "event_id": f"event-{sequence}",
        "event_type": event_type,
        "venue": "BINANCE",
        "instrument_id": "BTC-USDT-SPOT",
        "exchange_ts_ns": receive_ts_ns,
        "receive_ts_ns": receive_ts_ns,
        "sequence": sequence,
        "bids": bids,
        "asks": asks,
    })


class FakeDatagramSocket:
    def __init__(self):
        self.options = []
        self.blocking = None
        self.sent = []
        self.closed = False

    def setsockopt(self, *args):
        self.options.append(args)

    def setblocking(self, value):
        self.blocking = value

    def sendto(self, data, destination):
        self.sent.append((data, destination))
        return len(data)

    def close(self):
        self.closed = True


class MarketDataOutputTests(unittest.TestCase):
    def test_public_trades_normalize_and_ltp_resamples_exact_one_minute_ohlc(self):
        payloads = [
            {"e": "trade", "s": "BTCUSDT", "t": index, "T": timestamp_ms,
             "p": price, "q": quantity, "m": False}
            for index, timestamp_ms, price, quantity in (
                (1, 1000, "100", "2"),
                (2, 2000, "103", "1"),
                (3, 3000, "99", "0.5"),
                (4, 61000, "101", "3"),
            )
        ]
        receive_times = (1, 2, 3, 61)
        ticks = [
            normalize_binance_trade(
                payload, receive * 1_000_000_000, "BTC-USDT-SPOT", f"raw-{receive}"
            )
            for payload, receive in zip(payloads, receive_times)
        ]
        resampler = LtpOhlcResampler(60)
        completed = []
        for tick in ticks:
            completed.extend(resampler.update(tick))

        self.assertEqual(len(completed), 1)
        bar = completed[0]
        self.assertEqual((bar.open, bar.high, bar.low, bar.close), (
            Decimal("100"), Decimal("103"), Decimal("99"), Decimal("99")
        ))
        self.assertEqual(bar.trade_count, 3)
        self.assertEqual(bar.base_volume, Decimal("3.5"))
        self.assertEqual(bar.quote_volume, Decimal("352.5"))
        self.assertTrue(bar.is_final)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ltp_ohlc_1m.csv"
            writer = LtpOhlcCsvWriter(path, durable=False)
            writer.append(bar)
            result = writer.close()
            self.assertEqual(result["rows"], 1)
            self.assertIn("trade_count", path.read_text().splitlines()[0])

    def test_bybit_trade_frame_can_normalize_multiple_ticks(self):
        ticks = normalize_bybit_trades({
            "topic": "publicTrade.BTCUSDT",
            "data": [
                {"s": "BTCUSDT", "i": "a", "T": 1000, "p": "100", "v": "1", "S": "Buy"},
                {"s": "BTCUSDT", "i": "b", "T": 1001, "p": "101", "v": "2", "S": "Sell"},
            ],
        }, 2_000_000_000, "BTC-USDT-SPOT", "raw-bybit")
        self.assertEqual(len(ticks), 2)
        self.assertEqual([tick.aggressor_side for tick in ticks], ["BUY", "SELL"])

    def test_one_minute_resampling_uses_valid_reconstructed_top_of_book(self):
        book = OrderBook("BINANCE", "BTC-USDT-SPOT")
        resampler = OrderBookOhlcResampler(60)
        samples = [
            event(1, 1_000_000_000, [["100", "1"]], [["102", "1"]], EventType.BOOK_SNAPSHOT),
            event(2, 30_000_000_000, [["101", "1"]], [["102", "0"], ["103", "1"]]),
            event(3, 61_000_000_000, [["102", "1"]], [["103", "0"], ["104", "1"]]),
        ]
        completed = []
        for sample in samples:
            book.apply(sample)
            completed.extend(resampler.update(book, sample))

        self.assertEqual(len(completed), 1)
        first = completed[0]
        self.assertTrue(first.is_final)
        self.assertEqual(first.sample_count, 2)
        self.assertEqual(str(first.mid_open), "101")
        self.assertEqual(str(first.mid_high), "102")
        self.assertEqual(str(first.mid_low), "101")
        self.assertEqual(str(first.mid_close), "102")
        self.assertEqual(str(first.spread_close), "2")

        partial = resampler.flush_partial()
        self.assertEqual(len(partial), 1)
        self.assertFalse(partial[0].is_final)
        self.assertEqual(partial[0].open_ts_ns, 60_000_000_000)

    def test_csv_dump_contains_final_and_partial_bars(self):
        book = OrderBook("BINANCE", "BTC-USDT-SPOT")
        resampler = OrderBookOhlcResampler()
        sample = event(
            1, 1_000_000_000, [["100", "1"]], [["102", "1"]],
            EventType.BOOK_SNAPSHOT,
        )
        book.apply(sample)
        resampler.update(book, sample)
        bar = resampler.flush_partial()[0]

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ohlc_1m.csv"
            writer = OhlcCsvWriter(path, durable=False)
            writer.append(bar)
            result = writer.close()
            rows = path.read_text().splitlines()
            self.assertEqual(result["rows"], 1)
            self.assertEqual(len(rows), 2)
            self.assertIn("mid_open", rows[0])
            self.assertIn("False", rows[1])

    def test_udp_multicast_publishes_typed_book_and_ohlc_datagrams(self):
        fake = FakeDatagramSocket()

        def socket_factory(*_):
            return fake

        publisher = UdpMulticastPublisher(
            UdpPublisherSettings(depth=2), socket_factory=socket_factory
        )
        book = OrderBook("BINANCE", "BTC-USDT-SPOT")
        sample = event(
            1, 1_000_000_000,
            [["100", "1"], ["99", "2"], ["98", "3"]],
            [["102", "1"], ["103", "2"], ["104", "3"]],
            EventType.BOOK_SNAPSHOT,
        )
        book.apply(sample)
        resampler = OrderBookOhlcResampler()
        resampler.update(book, sample)
        bar = resampler.flush_partial()[0]

        self.assertTrue(publisher.publish_orderbook(book, sample, 1_100_000_000))
        self.assertTrue(publisher.publish_ohlc(bar, 60_000_000_000))
        tick = normalize_binance_trade(
            {"e": "trade", "s": "BTCUSDT", "t": 1, "T": 1000,
             "p": "101", "q": "0.5", "m": True},
            2_000_000_000, "BTC-USDT-SPOT", "raw-1",
        )
        ltp = LtpOhlcResampler()
        ltp.update(tick)
        ltp_bar = ltp.flush_partial()[0]
        self.assertTrue(publisher.publish_trade(tick, 2_100_000_000))
        self.assertTrue(publisher.publish_ltp_ohlc(ltp_bar, 60_000_000_000))
        messages = [json.loads(data) for data, _ in fake.sent]
        self.assertEqual([x["message_type"] for x in messages], [
            "ORDERBOOK", "OHLC_1M", "TRADE", "LTP_OHLC_1M"
        ])
        self.assertEqual([x["publisher_sequence"] for x in messages], [1, 2, 3, 4])
        self.assertEqual(len(messages[0]["payload"]["bids"]), 2)
        self.assertEqual(fake.sent[0][1], ("239.255.77.77", 19001))
        self.assertFalse(fake.blocking)
        publisher.close()
        self.assertTrue(fake.closed)

    def test_oversized_udp_message_is_dropped_without_blocking(self):
        fake = FakeDatagramSocket()
        publisher = UdpMulticastPublisher(
            UdpPublisherSettings(max_datagram_bytes=100),
            socket_factory=lambda *_: fake,
        )
        book = OrderBook("BINANCE", "BTC-USDT-SPOT")
        sample = event(
            1, 1_000_000_000, [["100", "1"]], [["102", "1"]],
            EventType.BOOK_SNAPSHOT,
        )
        book.apply(sample)
        self.assertFalse(publisher.publish_orderbook(book, sample, 1_100_000_000))
        self.assertEqual(publisher.stats.dropped_messages, 1)
        self.assertEqual(fake.sent, [])


if __name__ == "__main__":
    unittest.main()
