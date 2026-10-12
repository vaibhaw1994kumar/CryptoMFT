"""Coverage for the market-data sanity checks added for feed integrity."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest

from trading_platform_poc.config import DataQualityConfig
from trading_platform_poc.data_quality import (
    AnomalyCode,
    AnomalySeverity,
    DataQualityValidator,
)
from trading_platform_poc.models import (
    BookDelta,
    BookSnapshot,
    EventType,
    MarketEvent,
    PriceLevel,
)


RUN_ID = UUID("00000000-0000-0000-0000-000000000001")
EVENT_ID = UUID("00000000-0000-0000-0000-000000000002")
BASE_NS = 1_000_000_000


def levels(*pairs: tuple[str, str]) -> tuple[PriceLevel, ...]:
    return tuple(
        PriceLevel(Decimal(price), Decimal(quantity))
        for price, quantity in pairs
    )


def snapshot_event(
    bids: tuple[PriceLevel, ...],
    asks: tuple[PriceLevel, ...],
    *,
    last_update_id: int = 1,
    receive_timestamp_ns: int = BASE_NS,
    process_timestamp_ns: int | None = None,
    symbol: str = "BTCUSDT",
) -> MarketEvent:
    return MarketEvent(
        event_id=EVENT_ID,
        run_id=RUN_ID,
        venue="binance",
        symbol=symbol,
        event_type=EventType.BOOK_SNAPSHOT,
        exchange_sequence=last_update_id,
        exchange_timestamp_ns=receive_timestamp_ns,
        receive_timestamp_ns=receive_timestamp_ns,
        process_timestamp_ns=(
            process_timestamp_ns
            if process_timestamp_ns is not None
            else receive_timestamp_ns + 1
        ),
        schema_version=1,
        payload=BookSnapshot(symbol, last_update_id, bids, asks),
    )


def delta_event(
    bids: tuple[PriceLevel, ...],
    asks: tuple[PriceLevel, ...],
    *,
    first_update_id: int = 2,
    final_update_id: int = 3,
) -> MarketEvent:
    return MarketEvent(
        event_id=EVENT_ID,
        run_id=RUN_ID,
        venue="binance",
        symbol="BTCUSDT",
        event_type=EventType.BOOK_DELTA,
        exchange_sequence=final_update_id,
        exchange_timestamp_ns=BASE_NS,
        receive_timestamp_ns=BASE_NS,
        process_timestamp_ns=BASE_NS + 1,
        schema_version=1,
        payload=BookDelta(
            "BTCUSDT",
            first_update_id,
            final_update_id,
            bids,
            asks,
        ),
    )


def validator(**overrides: object) -> DataQualityValidator:
    return DataQualityValidator(DataQualityConfig(**overrides))


def codes(check: DataQualityValidator) -> list[AnomalyCode]:
    return [anomaly.code for anomaly in check.anomalies]


# ----------------------------------------------------------------------
# crossed and locked markets
# ----------------------------------------------------------------------


def test_crossed_snapshot_is_rejected() -> None:
    check = validator()
    accepted = check.validate_event(
        snapshot_event(
            levels(("101", "1")),
            levels(("100", "1")),
        )
    )
    assert accepted is False
    assert AnomalyCode.CROSSED_BOOK in codes(check)
    anomaly = next(
        item
        for item in check.anomalies
        if item.code is AnomalyCode.CROSSED_BOOK
    )
    assert anomaly.severity is AnomalySeverity.CRITICAL
    assert anomaly.blocking is True
    assert anomaly.details["best_bid"] == "101"
    assert anomaly.details["best_ask"] == "100"


def test_locked_market_is_flagged() -> None:
    check = validator()
    assert (
        check.validate_event(
            snapshot_event(levels(("100", "1")), levels(("100", "1")))
        )
        is False
    )
    assert AnomalyCode.CROSSED_BOOK in codes(check)


def test_crossed_book_can_be_downgraded_to_a_warning() -> None:
    check = validator(reject_crossed_books=False)
    assert (
        check.validate_event(
            snapshot_event(levels(("101", "1")), levels(("100", "1")))
        )
        is True
    )
    assert AnomalyCode.CROSSED_BOOK in codes(check)


def test_uncrossed_snapshot_passes() -> None:
    check = validator()
    assert (
        check.validate_event(
            snapshot_event(
                levels(("100", "1"), ("99", "2")),
                levels(("101", "1"), ("102", "2")),
            )
        )
        is True
    )
    assert check.anomalies == []


# ----------------------------------------------------------------------
# level ordering and duplicates
# ----------------------------------------------------------------------


def test_bids_must_descend() -> None:
    check = validator()
    assert (
        check.validate_event(
            snapshot_event(
                levels(("99", "1"), ("100", "1")),
                levels(("101", "1")),
            )
        )
        is False
    )
    assert AnomalyCode.BOOK_LEVEL_ORDERING in codes(check)
    anomaly = next(
        item
        for item in check.anomalies
        if item.code is AnomalyCode.BOOK_LEVEL_ORDERING
    )
    assert anomaly.details["side"] == "bid"


def test_asks_must_ascend() -> None:
    check = validator()
    assert (
        check.validate_event(
            snapshot_event(
                levels(("100", "1")),
                levels(("102", "1"), ("101", "1")),
            )
        )
        is False
    )
    anomaly = next(
        item
        for item in check.anomalies
        if item.code is AnomalyCode.BOOK_LEVEL_ORDERING
    )
    assert anomaly.details["side"] == "ask"


def test_duplicate_price_levels_are_rejected() -> None:
    check = validator()
    assert (
        check.validate_event(
            snapshot_event(
                levels(("100", "1"), ("100", "2")),
                levels(("101", "1")),
            )
        )
        is False
    )
    assert AnomalyCode.DUPLICATE_BOOK_LEVEL in codes(check)


def test_deltas_reject_duplicates_but_allow_unsorted_updates() -> None:
    check = validator()
    # Establish snapshot baseline so delta sequence check does not block.
    check.validate_event(
        snapshot_event(levels(("100", "1")), levels(("101", "1")), last_update_id=1)
    )
    # Deltas legitimately carry removals, so ordering is not enforced.
    assert (
        check.validate_event(
            delta_event(
                levels(("99", "0"), ("100", "1")),
                levels(("101", "1")),
            )
        )
        is True
    )
    assert AnomalyCode.BOOK_LEVEL_ORDERING not in codes(check)

    assert (
        check.validate_event(
            delta_event(
                levels(("100", "1"), ("100", "0")),
                levels(("101", "1")),
                first_update_id=4,
                final_update_id=5,
            )
        )
        is False
    )
    assert AnomalyCode.DUPLICATE_BOOK_LEVEL in codes(check)


def test_delta_is_never_flagged_as_crossed() -> None:
    check = validator()
    # Establish snapshot baseline so the delta sequence check does not block.
    check.validate_event(
        snapshot_event(levels(("100", "1")), levels(("101", "1")), last_update_id=1)
    )
    # A delta showing only one side of a move is not evidence of a cross.
    assert (
        check.validate_event(
            delta_event(levels(("101", "1")), levels(("100", "1")))
        )
        is True
    )
    assert AnomalyCode.CROSSED_BOOK not in codes(check)


# ----------------------------------------------------------------------
# processing latency
# ----------------------------------------------------------------------


def test_excessive_processing_latency_is_reported() -> None:
    check = validator(max_processing_latency_ms=5)
    accepted = check.validate_event(
        snapshot_event(
            levels(("100", "1")),
            levels(("101", "1")),
            process_timestamp_ns=BASE_NS + 10_000_000,
        )
    )
    # Warning by default: the data itself is still usable.
    assert accepted is True
    anomaly = next(
        item
        for item in check.anomalies
        if item.code is AnomalyCode.EXCESSIVE_PROCESSING_LATENCY
    )
    assert anomaly.severity is AnomalySeverity.WARNING
    assert anomaly.blocking is False
    assert anomaly.details["processing_latency_ns"] == "10000000"


def test_processing_latency_can_be_made_blocking() -> None:
    check = validator(
        max_processing_latency_ms=5,
        reject_processing_latency=True,
    )
    assert (
        check.validate_event(
            snapshot_event(
                levels(("100", "1")),
                levels(("101", "1")),
                process_timestamp_ns=BASE_NS + 10_000_000,
            )
        )
        is False
    )


def test_normal_processing_latency_is_silent() -> None:
    check = validator(max_processing_latency_ms=50)
    assert (
        check.validate_event(
            snapshot_event(levels(("100", "1")), levels(("101", "1")))
        )
        is True
    )
    assert AnomalyCode.EXCESSIVE_PROCESSING_LATENCY not in codes(check)


# ----------------------------------------------------------------------
# heartbeat monitoring
# ----------------------------------------------------------------------


def test_heartbeat_timeout_detects_a_silent_feed() -> None:
    check = validator(max_heartbeat_interval_ms=100)
    check.validate_event(
        snapshot_event(levels(("100", "1")), levels(("101", "1")))
    )

    assert check.check_heartbeats(BASE_NS + 50_000_000) == ()

    reported = check.check_heartbeats(BASE_NS + 200_000_000)
    assert len(reported) == 1
    anomaly = reported[0]
    assert anomaly.code is AnomalyCode.FEED_HEARTBEAT_TIMEOUT
    assert anomaly.severity is AnomalySeverity.CRITICAL
    assert anomaly.blocking is True
    assert anomaly.symbol == "BTCUSDT"
    assert anomaly.details["silence_ns"] == "200000000"


def test_heartbeat_tracks_each_symbol_independently() -> None:
    check = validator(max_heartbeat_interval_ms=100)
    check.validate_event(
        snapshot_event(levels(("100", "1")), levels(("101", "1")))
    )
    check.record_heartbeat("ETHUSDT", BASE_NS + 190_000_000)

    reported = check.check_heartbeats(BASE_NS + 200_000_000)
    assert [anomaly.symbol for anomaly in reported] == ["BTCUSDT"]


def test_heartbeat_scope_can_be_restricted() -> None:
    check = validator(max_heartbeat_interval_ms=100)
    check.record_heartbeat("BTCUSDT", BASE_NS)
    check.record_heartbeat("ETHUSDT", BASE_NS)

    reported = check.check_heartbeats(
        BASE_NS + 200_000_000,
        symbols=("ethusdt",),
    )
    assert [anomaly.symbol for anomaly in reported] == ["ETHUSDT"]


def test_unknown_symbols_do_not_raise() -> None:
    check = validator()
    assert check.check_heartbeats(BASE_NS, symbols=("NOPE",)) == ()


def test_heartbeat_resets_when_data_resumes() -> None:
    check = validator(max_heartbeat_interval_ms=100)
    check.record_heartbeat("BTCUSDT", BASE_NS)
    assert check.check_heartbeats(BASE_NS + 200_000_000)
    check.record_heartbeat("BTCUSDT", BASE_NS + 200_000_000)
    assert check.check_heartbeats(BASE_NS + 250_000_000) == ()


# ----------------------------------------------------------------------
# configuration
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    ["max_processing_latency_ms", "max_heartbeat_interval_ms"],
)
def test_new_thresholds_must_be_positive(field: str) -> None:
    with pytest.raises(ValueError):
        DataQualityConfig(**{field: 0})


def test_defaults_are_documented_values() -> None:
    config = DataQualityConfig()
    assert config.max_processing_latency_ms == 50
    assert config.max_heartbeat_interval_ms == 5000
    assert config.reject_crossed_books is True
    assert config.reject_processing_latency is False


def test_every_anomaly_code_is_unique() -> None:
    values = [code.value for code in AnomalyCode]
    assert len(values) == len(set(values))


def test_heartbeat_state_survives_capture_and_restore() -> None:
    check = validator(max_heartbeat_interval_ms=100)
    check.record_heartbeat("BTCUSDT", BASE_NS)
    state = check.capture_state()

    replay = validator(max_heartbeat_interval_ms=100)
    replay.restore_state(state)
    reported = replay.check_heartbeats(BASE_NS + 200_000_000)
    assert [anomaly.symbol for anomaly in reported] == ["BTCUSDT"]
