from decimal import Decimal
from uuid import UUID

from trading_platform_poc.config import (
    AppConfig,
    DataQualityConfig,
    RiskConfig,
    StrategyConfig,
)
from trading_platform_poc.data_quality import (
    AnomalyCode,
    DataQualityValidator,
)
from trading_platform_poc.engine import TradingEngine
from trading_platform_poc.models import (
    BookDelta,
    BookSnapshot,
    EventType,
    MarketEvent,
    PriceLevel,
    TradeTick,
)
from trading_platform_poc.order_book import BinanceOrderBook
from trading_platform_poc.reporting import write_report


RUN_ID = UUID("00000000-0000-0000-0000-000000000001")
EVENT_ID = UUID("00000000-0000-0000-0000-000000000002")


def trade_event(
    trade_id: int,
    *,
    price: str = "100",
    quantity: str = "0.1",
    exchange_timestamp_ns: int = 1_000_000_000,
    receive_timestamp_ns: int = 1_000_000_000,
) -> MarketEvent:
    return MarketEvent(
        event_id=EVENT_ID,
        run_id=RUN_ID,
        venue="binance",
        symbol="BTCUSDT",
        event_type=EventType.TRADE,
        exchange_sequence=trade_id,
        exchange_timestamp_ns=exchange_timestamp_ns,
        receive_timestamp_ns=receive_timestamp_ns,
        process_timestamp_ns=receive_timestamp_ns + 1,
        schema_version=1,
        payload=TradeTick(
            "BTCUSDT",
            trade_id,
            Decimal(price),
            Decimal(quantity),
            False,
            exchange_timestamp_ns,
        ),
    )


def snapshot_event(
    *,
    sequence: int = 10,
    exchange_timestamp_ns: int = 1_000_000_000,
    receive_timestamp_ns: int = 1_000_000_000,
) -> MarketEvent:
    return MarketEvent(
        EVENT_ID,
        RUN_ID,
        "binance",
        "BTCUSDT",
        EventType.BOOK_SNAPSHOT,
        sequence,
        exchange_timestamp_ns,
        receive_timestamp_ns,
        receive_timestamp_ns + 1,
        1,
        BookSnapshot(
            "BTCUSDT",
            sequence,
            (PriceLevel(Decimal("100"), Decimal("1")),),
            (PriceLevel(Decimal("101"), Decimal("1")),),
        ),
    )


def test_rejects_future_stale_and_regressing_timestamps() -> None:
    future = DataQualityValidator(
        DataQualityConfig(max_future_skew_ms=10)
    )
    assert (
        future.validate_event(
            trade_event(
                1,
                exchange_timestamp_ns=2_000_000_000,
                receive_timestamp_ns=1_000_000_000,
            )
        )
        is False
    )
    assert future.anomalies[-1].code is AnomalyCode.FUTURE_TIMESTAMP
    assert future.validate_event(trade_event(1)) is True

    stale = DataQualityValidator(
        DataQualityConfig(max_market_data_delay_ms=100)
    )
    assert (
        stale.validate_event(
            trade_event(
                1,
                exchange_timestamp_ns=1_000_000_000,
                receive_timestamp_ns=1_500_000_000,
            )
        )
        is False
    )
    assert stale.anomalies[-1].code is AnomalyCode.STALE_MARKET_DATA

    regression = DataQualityValidator(DataQualityConfig())
    assert regression.validate_event(trade_event(1, exchange_timestamp_ns=2_000_000_000))
    assert (
        regression.validate_event(
            trade_event(
                2,
                exchange_timestamp_ns=1_000_000_000,
                receive_timestamp_ns=2_000_000_000,
            )
        )
        is False
    )
    assert any(
        anomaly.code is AnomalyCode.TIMESTAMP_REGRESSION
        for anomaly in regression.anomalies
    )


def test_rejects_invalid_processing_and_trade_timestamp_ordering() -> None:
    validator = DataQualityValidator(
        DataQualityConfig(max_trade_timestamp_difference_ms=10)
    )
    event = trade_event(1)
    invalid_order = MarketEvent(
        event.event_id,
        event.run_id,
        event.venue,
        event.symbol,
        event.event_type,
        event.exchange_sequence,
        event.exchange_timestamp_ns,
        event.receive_timestamp_ns,
        event.receive_timestamp_ns - 1,
        event.schema_version,
        event.payload,
    )
    assert validator.validate_event(invalid_order) is False
    assert any(
        anomaly.code is AnomalyCode.TIMESTAMP_ORDERING
        for anomaly in validator.anomalies
    )

    mismatch_payload = TradeTick(
        "BTCUSDT",
        2,
        Decimal("100"),
        Decimal("0.1"),
        False,
        2_000_000_000,
    )
    mismatch = MarketEvent(
        event.event_id,
        event.run_id,
        event.venue,
        event.symbol,
        event.event_type,
        2,
        1_000_000_000,
        1_000_000_000,
        1_000_000_001,
        1,
        mismatch_payload,
    )
    assert validator.validate_event(mismatch) is False
    assert any(
        anomaly.code is AnomalyCode.TRADE_TIMESTAMP_MISMATCH
        for anomaly in validator.anomalies
    )


def test_rejects_duplicate_trade_and_invalid_quantity() -> None:
    validator = DataQualityValidator(
        DataQualityConfig(max_trade_quantity=Decimal("1"))
    )

    assert validator.validate_event(trade_event(10))
    assert validator.validate_event(
        trade_event(
            10,
            exchange_timestamp_ns=1_000_000_001,
            receive_timestamp_ns=1_000_000_001,
        )
    ) is False
    assert validator.anomalies[-1].code is AnomalyCode.DUPLICATE_TRADE

    assert validator.validate_event(
        trade_event(
            11,
            quantity="2",
            exchange_timestamp_ns=1_000_000_002,
            receive_timestamp_ns=1_000_000_002,
        )
    ) is False
    assert validator.anomalies[-1].code is AnomalyCode.INVALID_QUANTITY


def test_rejects_symbol_sequence_and_non_finite_values() -> None:
    validator = DataQualityValidator(DataQualityConfig())
    base = trade_event(1)
    mismatched = MarketEvent(
        base.event_id,
        base.run_id,
        base.venue,
        base.symbol,
        base.event_type,
        base.exchange_sequence,
        base.exchange_timestamp_ns,
        base.receive_timestamp_ns,
        base.process_timestamp_ns,
        base.schema_version,
        TradeTick(
            "ETHUSDT",
            1,
            Decimal("100"),
            Decimal("0.1"),
            False,
            base.exchange_timestamp_ns,
        ),
    )
    bad_sequence = MarketEvent(
        base.event_id,
        base.run_id,
        base.venue,
        base.symbol,
        base.event_type,
        -1,
        base.exchange_timestamp_ns,
        base.receive_timestamp_ns,
        base.process_timestamp_ns,
        base.schema_version,
        base.payload,
    )
    non_finite = trade_event(3, price="NaN", quantity="Infinity")

    assert validator.validate_event(mismatched) is False
    assert validator.anomalies[-1].code is AnomalyCode.SYMBOL_MISMATCH
    assert validator.validate_event(bad_sequence) is False
    assert validator.anomalies[-1].code is AnomalyCode.INVALID_SEQUENCE
    assert validator.validate_event(non_finite) is False
    assert {
        anomaly.code for anomaly in validator.anomalies
    } >= {
        AnomalyCode.INVALID_PRICE,
        AnomalyCode.INVALID_QUANTITY,
    }


def test_detects_abnormal_price_move_without_rejecting_by_default() -> None:
    validator = DataQualityValidator(
        DataQualityConfig(
            max_price_move_bps=Decimal("10"),
            reject_abnormal_price_moves=False,
        )
    )

    assert validator.validate_event(trade_event(1, price="100"))
    assert validator.validate_event(
        trade_event(
            2,
            price="101",
            exchange_timestamp_ns=1_000_000_001,
            receive_timestamp_ns=1_000_000_001,
        )
    )
    assert validator.anomalies[-1].code is AnomalyCode.ABNORMAL_PRICE_MOVE
    assert validator.anomalies[-1].blocking is False


def test_validates_depth_spread_and_local_checksum_consistency() -> None:
    validator = DataQualityValidator(
        DataQualityConfig(
            min_book_depth=2,
            max_spread_bps=Decimal("10"),
        )
    )
    book = BinanceOrderBook("BTCUSDT")
    event = snapshot_event()
    book.load_snapshot(event.payload, event.receive_timestamp_ns)

    assert validator.validate_book(book, event) is False
    assert {
        anomaly.code for anomaly in validator.anomalies
    } >= {
        AnomalyCode.INSUFFICIENT_BOOK_DEPTH,
        AnomalyCode.EXCESSIVE_SPREAD,
    }

    checksum_validator = DataQualityValidator(
        DataQualityConfig(max_spread_bps=Decimal("1000"))
    )
    checksum_validator.validate_book(book, event)
    changed = BookSnapshot(
        "BTCUSDT",
        10,
        (PriceLevel(Decimal("100"), Decimal("2")),),
        (PriceLevel(Decimal("101"), Decimal("1")),),
    )
    book.load_snapshot(changed, 2_000_000_000)

    assert checksum_validator.validate_book(book, event) is False
    assert (
        checksum_validator.anomalies[-1].code
        is AnomalyCode.BOOK_CHECKSUM_MISMATCH
    )


def test_book_checksum_history_is_bounded() -> None:
    validator = DataQualityValidator(
        DataQualityConfig(
            max_spread_bps=Decimal("1000"),
            max_retained_book_checksums=2,
        )
    )
    book = BinanceOrderBook("BTCUSDT")

    for sequence in range(1, 4):
        event = snapshot_event(sequence=sequence)
        book.load_snapshot(event.payload, event.receive_timestamp_ns)
        assert validator.validate_book(book, event)

    assert len(validator._book_checksums) == 2


def test_alert_queue_receives_structured_anomalies() -> None:
    validator = DataQualityValidator(
        DataQualityConfig(max_trade_quantity=Decimal("1"))
    )

    validator.validate_event(trade_event(1, quantity="2"))

    received = validator.alerts.get_nowait()
    assert received is validator.anomalies[0]
    assert received.to_dict()["code"] == "invalid_quantity"


def test_anomaly_retention_is_bounded_while_counters_remain_complete() -> None:
    validator = DataQualityValidator(
        DataQualityConfig(
            max_trade_quantity=Decimal("1"),
            max_retained_anomalies=2,
        ),
    )

    for trade_id in range(1, 4):
        validator.validate_event(
            trade_event(
                trade_id,
                quantity="2",
                exchange_timestamp_ns=1_000_000_000 + trade_id,
                receive_timestamp_ns=1_000_000_000 + trade_id,
            )
        )

    assert len(validator.anomalies) == 2
    assert validator.summary()["count"] == 3
    assert validator.summary()["retained_count"] == 2
    assert validator.alerts.qsize() == 2


def test_engine_blocks_unsafe_delta_before_strategy() -> None:
    config = AppConfig(
        data_quality=DataQualityConfig(max_market_data_delay_ms=100),
        risk=RiskConfig(max_net_position=Decimal("1")),
        strategy=StrategyConfig(
            spread_threshold_bps=Decimal("1"),
            order_notional=Decimal("10"),
            quote_cooldown_ms=0,
        ),
    )
    engine = TradingEngine(config)
    snapshot = snapshot_event()
    assert engine.process(snapshot)
    delayed_delta = MarketEvent(
        EVENT_ID,
        RUN_ID,
        "binance",
        "BTCUSDT",
        EventType.BOOK_DELTA,
        11,
        1_000_000_000,
        1_500_000_000,
        1_500_000_001,
        1,
        BookDelta("BTCUSDT", 11, 11, (), ()),
    )

    assert engine.process(delayed_delta) is False
    assert engine.opportunities == []
    assert (
        engine.data_quality.anomalies[-1].code
        is AnomalyCode.STALE_MARKET_DATA
    )


def test_engine_reports_duplicate_book_updates() -> None:
    engine = TradingEngine(AppConfig())
    snapshot = snapshot_event()
    assert engine.process(snapshot)
    stale_delta = MarketEvent(
        EVENT_ID,
        RUN_ID,
        "binance",
        "BTCUSDT",
        EventType.BOOK_DELTA,
        9,
        1_000_000_001,
        1_000_000_001,
        1_000_000_002,
        1,
        BookDelta("BTCUSDT", 9, 9, (), ()),
    )

    assert engine.process(stale_delta) is False
    assert (
        engine.data_quality.anomalies[-1].code
        is AnomalyCode.OUT_OF_ORDER_BOOK_UPDATE
    )


def test_rejected_book_update_does_not_poison_timestamp_state() -> None:
    engine = TradingEngine(AppConfig())
    assert engine.process(snapshot_event())
    stale = MarketEvent(
        EVENT_ID,
        RUN_ID,
        "binance",
        "BTCUSDT",
        EventType.BOOK_DELTA,
        9,
        2_000_000_000,
        2_000_000_000,
        2_000_000_001,
        1,
        BookDelta("BTCUSDT", 9, 9, (), ()),
    )
    valid = MarketEvent(
        EVENT_ID,
        RUN_ID,
        "binance",
        "BTCUSDT",
        EventType.BOOK_DELTA,
        11,
        1_100_000_000,
        1_100_000_000,
        1_100_000_001,
        1,
        BookDelta("BTCUSDT", 11, 11, (), ()),
    )

    assert engine.process(stale) is False
    assert engine.process(valid) is True
    assert engine.book.last_update_id == 11
    assert not any(
        anomaly.code is AnomalyCode.TIMESTAMP_REGRESSION
        for anomaly in engine.data_quality.anomalies
    )


def test_engine_discards_book_rejected_by_depth_policy_until_snapshot() -> None:
    config = AppConfig(
        data_quality=DataQualityConfig(
            min_book_depth=2,
            max_spread_bps=Decimal("1000"),
        )
    )
    engine = TradingEngine(config)
    snapshot = MarketEvent(
        EVENT_ID,
        RUN_ID,
        "binance",
        "BTCUSDT",
        EventType.BOOK_SNAPSHOT,
        10,
        1_000_000_000,
        1_000_000_000,
        1_000_000_001,
        1,
        BookSnapshot(
            "BTCUSDT",
            10,
            (
                PriceLevel(Decimal("100"), Decimal("1")),
                PriceLevel(Decimal("99"), Decimal("1")),
            ),
            (
                PriceLevel(Decimal("101"), Decimal("1")),
                PriceLevel(Decimal("102"), Decimal("1")),
            ),
        ),
    )
    assert engine.process(snapshot)
    rejected = MarketEvent(
        EVENT_ID,
        RUN_ID,
        "binance",
        "BTCUSDT",
        EventType.BOOK_DELTA,
        11,
        1_000_000_001,
        1_000_000_001,
        1_000_000_002,
        1,
        BookDelta(
            "BTCUSDT",
            11,
            11,
            (PriceLevel(Decimal("99"), Decimal("0")),),
            (),
        ),
    )

    assert engine.process(rejected) is False
    assert not engine.market_data_healthy
    assert not engine.book.initialized

    recovery = MarketEvent(
        EVENT_ID,
        RUN_ID,
        "binance",
        "BTCUSDT",
        EventType.BOOK_SNAPSHOT,
        11,
        1_000_000_003,
        1_000_000_003,
        1_000_000_004,
        1,
        BookSnapshot(
            "BTCUSDT",
            11,
            (
                PriceLevel(Decimal("100"), Decimal("1")),
                PriceLevel(Decimal("99"), Decimal("1")),
            ),
            (
                PriceLevel(Decimal("101"), Decimal("1")),
                PriceLevel(Decimal("102"), Decimal("1")),
            ),
        ),
    )
    assert engine.process(recovery)
    assert engine.market_data_healthy
    assert engine.book.last_update_id == 11


def test_anomalies_are_written_to_run_report(tmp_path) -> None:
    engine = TradingEngine(
        AppConfig(
            data_quality=DataQualityConfig(max_market_data_delay_ms=100)
        )
    )
    assert engine.process(snapshot_event())
    assert engine.process(
        trade_event(
            1,
            exchange_timestamp_ns=1_000_000_000,
            receive_timestamp_ns=1_500_000_000,
        )
    ) is False

    summary = write_report(
        tmp_path / "report",
        RUN_ID,
        "test",
        engine,
    )

    anomaly_lines = (
        tmp_path / "report" / "anomalies.jsonl"
    ).read_text(encoding="utf-8").splitlines()
    assert len(anomaly_lines) == 1
    assert summary["data_quality"]["blocking_count"] == 1
    assert summary["data_quality"]["by_code"] == {
        "stale_market_data": 1
    }
