"""Unit coverage for the minimum-requirements data plane (MR-01 to MR-06).

Every test is hermetic: the deterministic synthetic generator stands in for
exchange capture so the whole raw-to-canonical path runs without a socket.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from trading_platform_poc.minreq.canonical import (
    CanonicalEventType,
    QualityFlag,
    decode_event,
    encode_event,
    schema_document,
)
from trading_platform_poc.minreq.features import (
    CrossVenueFeatureEngine,
    FeatureConfig,
    LookaheadError,
    feature_catalogue,
    feature_set_hash,
)
from trading_platform_poc.minreq.fees import (
    CostModel,
    default_fee_book,
    walk_depth,
)
from trading_platform_poc.minreq.pipeline import (
    IngestionInterrupted,
    Pipeline,
    manifest_checksum,
    verify_manifest,
)
from trading_platform_poc.minreq.quality import reconstruct
from trading_platform_poc.minreq.replay import (
    MultiVenueReplay,
    assert_no_forward_leakage,
    replay_signature,
)
from trading_platform_poc.minreq.securitymaster import (
    InstrumentError,
    TradingStatus,
    default_security_master,
)
from trading_platform_poc.minreq.synthetic import (
    FaultInjection,
    SyntheticConfig,
    generate,
)
from trading_platform_poc.minreq.venues import (
    build_normalizer,
    supported_venues,
    venue_capability_matrix,
)

AT_NS = 1_800_000_000_000_000_000
BTC_BINANCE = "BTC-USDT.BINANCE.SPOT"
BTC_OKX = "BTC-USDT.OKX.SPOT"


@pytest.fixture(scope="module")
def small_dataset(tmp_path_factory: pytest.TempPathFactory) -> tuple[Pipeline, dict]:
    root = tmp_path_factory.mktemp("minreq-dataset")
    master = default_security_master()
    pipeline = Pipeline(root, master, dataset_id="unit")
    pipeline.ingest(generate(SyntheticConfig(seed=7, steps=60)))
    manifest = pipeline.run()
    return pipeline, manifest


# ------------------------------------------------------------------- MR-01/02
def test_every_supported_venue_normalizes_book_and_trade() -> None:
    master = default_security_master()
    records = generate(
        SyntheticConfig(seed=11, steps=8, venues=("binance", "okx", "bybit"))
    )
    seen: dict[str, set[CanonicalEventType]] = {}
    for record in records:
        normalizer = build_normalizer(record.venue, master)
        for event in normalizer.normalize(record):
            seen.setdefault(record.venue, set()).add(event.event_type)
            assert event.receive_timestamp_ns == record.receive_timestamp_ns
            assert event.native, "native venue fields must be preserved"
    assert set(seen) == {"binance", "okx", "bybit"}
    for venue, types in seen.items():
        assert CanonicalEventType.TRADE in types, venue
        assert types & {
            CanonicalEventType.BOOK_SNAPSHOT,
            CanonicalEventType.BOOK_DELTA,
        }, venue


def test_capability_matrix_covers_supported_venues() -> None:
    matrix = venue_capability_matrix()
    assert {row["venue"] for row in matrix} == set(supported_venues())
    for row in matrix:
        assert row["canonical_channels"], row["venue"]
        assert row["order_types"], row["venue"]
        assert row["time_in_force"], row["venue"]
        assert row["adapter_version"]


def test_canonical_events_round_trip_without_loss(small_dataset) -> None:
    pipeline, _ = small_dataset
    events = list(pipeline.canonical_events())
    assert events
    for event in events[:50]:
        restored = decode_event(encode_event(event))
        assert restored == event


def test_schema_document_pins_a_version() -> None:
    document = schema_document()
    assert document["schema_version"] >= 1
    assert document["event_types"]
    assert "ordering_key" in document


# ---------------------------------------------------------------------- MR-03
def test_security_master_is_effective_dated() -> None:
    master = default_security_master()
    record = master.resolve(BTC_BINANCE, AT_NS)
    assert record.venue_symbol == "BTCUSDT"
    suspended = master.with_status(
        BTC_BINANCE, TradingStatus.SUSPENDED, AT_NS + 1, source="test"
    )
    # The original master is immutable; only the derived one sees the halt.
    assert master.resolve(BTC_BINANCE, AT_NS + 2).tradable
    assert not suspended.resolve(BTC_BINANCE, AT_NS + 2).tradable
    assert suspended.resolve(BTC_BINANCE, AT_NS - 1).tradable


def test_security_master_rejects_unknown_instrument() -> None:
    master = default_security_master()
    with pytest.raises(InstrumentError):
        master.resolve("NOPE.BINANCE.SPOT", AT_NS)


def test_sizing_respects_tick_lot_and_minimum_notional() -> None:
    master = default_security_master()
    eligible = master.size_order(
        BTC_BINANCE,
        at_ns=AT_NS,
        side="buy",
        target_quantity=Decimal("0.01"),
        price=Decimal("60000.004"),
        reference_price=Decimal("60000.00"),
    )
    assert eligible.eligible
    assert eligible.price == Decimal("60000.00")

    too_small = master.size_order(
        BTC_BINANCE,
        at_ns=AT_NS,
        side="buy",
        target_quantity=Decimal("0.00006"),
        price=Decimal("60000.00"),
        reference_price=Decimal("60000.00"),
    )
    assert not too_small.eligible
    assert too_small.reason == "below_minimum_notional"


def test_economic_group_spans_venues() -> None:
    master = default_security_master()
    members = master.group_members("BTC/USDT.SPOT")
    assert BTC_BINANCE in members and BTC_OKX in members


# ---------------------------------------------------------------------- MR-09
def test_fee_book_is_effective_dated() -> None:
    from trading_platform_poc.minreq.canonical import Liquidity

    book = default_fee_book()
    master = default_security_master()
    instrument = master.resolve(BTC_BINANCE, AT_NS)
    before = book.resolve(instrument, Liquidity.TAKER, 1_700_000_000_000_000_000)
    after = book.resolve(instrument, Liquidity.TAKER, AT_NS)
    assert before.record_version == 1
    assert after.record_version == 2
    assert after.rate_bps < before.rate_bps


def test_depth_walk_prices_the_whole_order() -> None:
    from trading_platform_poc.minreq.canonical import PriceLevel

    levels = (
        PriceLevel(price=Decimal("100"), quantity=Decimal("1")),
        PriceLevel(price=Decimal("101"), quantity=Decimal("1")),
    )
    walk = walk_depth(levels, Decimal("1.5"), Decimal("100"), "buy")
    assert walk.filled_quantity == Decimal("1.5")
    assert walk.average_price == Decimal("100.3333333333333333333333333")
    assert walk.levels_consumed == 2
    assert walk.slippage_bps > 0


def test_cost_model_is_the_only_source_of_fee_arithmetic() -> None:
    from trading_platform_poc.minreq.canonical import Liquidity

    master = default_security_master()
    book = default_fee_book()
    model = CostModel(book)
    instrument = master.resolve(BTC_BINANCE, AT_NS)
    leg = model.leg_economics(
        instrument=instrument,
        side="buy",
        quantity=Decimal("0.1"),
        price=Decimal("60000"),
        reference_price=Decimal("60000"),
        at_ns=AT_NS,
        liquidity=Liquidity.TAKER,
    )
    assert leg.notional == Decimal("6000.0")
    expected = book.resolve(instrument, Liquidity.TAKER, AT_NS)
    assert leg.fee == expected.charge(leg.notional)
    assert leg.fee_bps == expected.effective_bps


# ------------------------------------------------------------------- MR-04/05
def test_dataset_manifest_is_reproducible_and_verifiable(small_dataset) -> None:
    _, manifest = small_dataset
    assert manifest["approved"] is True
    assert manifest["quality"]["failures"] == []
    assert manifest["checksum"] == manifest_checksum(manifest)


def test_verify_manifest_detects_tampering(tmp_path: Path) -> None:
    master = default_security_master()
    pipeline = Pipeline(tmp_path / "ds", master, dataset_id="tamper")
    pipeline.ingest(generate(SyntheticConfig(seed=13, steps=40)))
    pipeline.run()
    assert verify_manifest(tmp_path / "ds")["verified"] is True

    victim = next((tmp_path / "ds" / "canonical").rglob("events.jsonl"))
    victim.write_text(victim.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    result = verify_manifest(tmp_path / "ds")
    assert result["verified"] is False
    assert any(item.startswith("checksum:") for item in result["mismatches"])


def test_interrupted_ingestion_resumes_without_duplication(tmp_path: Path) -> None:
    master = default_security_master()
    records = generate(SyntheticConfig(seed=17, steps=50))

    reference = Pipeline(tmp_path / "ref", master, dataset_id="resume")
    reference.ingest(records)
    reference_manifest = reference.run()

    broken = Pipeline(tmp_path / "res", master, dataset_id="resume")
    with pytest.raises(IngestionInterrupted):
        broken.ingest(records, fail_after=len(records) // 2)

    resumed = Pipeline(tmp_path / "res", master, dataset_id="resume")
    resumed.ingest(records)
    resumed.backfill(records)
    resumed_manifest = resumed.run()

    assert resumed.ingestor.checkpoint.duplicates_skipped > 0
    assert resumed_manifest["raw_records"] == reference_manifest["raw_records"]
    assert resumed_manifest["checksum"] == reference_manifest["checksum"]


def test_book_reconstruction_is_deterministic(small_dataset) -> None:
    pipeline, manifest = small_dataset
    market_data = [
        event
        for event in pipeline.canonical_events()
        if event.event_type
        in {CanonicalEventType.BOOK_SNAPSHOT, CanonicalEventType.BOOK_DELTA}
    ]
    first = reconstruct(market_data)
    second = reconstruct(market_data)
    assert first.book_hashes == second.book_hashes
    assert first.book_hashes == manifest["quality"]["book_hashes"]


def test_injected_faults_are_flagged_and_recovered(tmp_path: Path) -> None:
    master = default_security_master()
    pipeline = Pipeline(tmp_path / "faulty", master, dataset_id="faulty")
    pipeline.ingest(
        generate(
            SyntheticConfig(
                seed=19,
                steps=120,
                faults=FaultInjection(
                    sequence_gap_at=30,
                    stale_gap_at=60,
                    crossed_book_at=80,
                    duplicate_at=100,
                ),
            )
        )
    )
    manifest = pipeline.run()
    codes = manifest["quality"]["by_code"]
    assert QualityFlag.SEQUENCE_GAP.value in codes
    assert QualityFlag.STALE.value in codes
    assert QualityFlag.CROSSED_BOOK.value in codes
    assert QualityFlag.DUPLICATE.value in codes
    # A defect that cannot recover would leave the book permanently invalid.
    assert codes[QualityFlag.SNAPSHOT_RESYNC.value] >= 2
    states = {
        stream: state["validity"]
        for stream, state in manifest["quality"]["book_states"].items()
    }
    assert set(states.values()) == {"valid"}
    assert manifest["approved"] is False


# ---------------------------------------------------------------------- MR-05
def test_replay_is_ordered_and_reproducible(small_dataset) -> None:
    pipeline, _ = small_dataset
    streams = pipeline.venue_streams()

    def once() -> list:
        replay = MultiVenueReplay(
            {venue: list(events) for venue, events in streams.items()},
            run_id="unit",
        )
        return list(replay.events())

    first, second = once(), once()
    assert [event.event_id for event in first] == [
        event.event_id for event in second
    ]
    assert_no_forward_leakage(first)
    assert replay_signature(first) == replay_signature(second)


# ---------------------------------------------------------------------- MR-06
def test_features_refuse_to_look_forward(small_dataset) -> None:
    pipeline, _ = small_dataset
    engine = CrossVenueFeatureEngine(
        FeatureConfig(economic_group="BTC/USDT.SPOT", min_samples=1),
        {"binance": BTC_BINANCE, "okx": BTC_OKX},
    )
    last_ns = 0
    for event in pipeline.canonical_events():
        if event.event_type not in {
            CanonicalEventType.BOOK_SNAPSHOT,
            CanonicalEventType.BOOK_DELTA,
        }:
            continue
        engine.observe(event)
        last_ns = event.receive_timestamp_ns
    assert engine.snapshot(last_ns) is not None
    with pytest.raises(LookaheadError):
        engine.snapshot(last_ns - 1)


def test_feature_catalogue_is_versioned() -> None:
    catalogue = feature_catalogue()
    assert catalogue["feature_set_version"]
    assert catalogue["feature_set_hash"] == feature_set_hash()
    assert catalogue["definitions"]
    for definition in catalogue["definitions"]:
        assert definition["description"]
        assert definition["inputs"]
        assert definition["lookback_ns"] >= 0


def test_feature_snapshot_is_json_serializable(small_dataset) -> None:
    pipeline, _ = small_dataset
    engine = CrossVenueFeatureEngine(
        FeatureConfig(economic_group="BTC/USDT.SPOT", min_samples=1),
        {"binance": BTC_BINANCE, "okx": BTC_OKX},
    )
    snapshot = None
    for event in pipeline.canonical_events():
        if event.event_type not in {
            CanonicalEventType.BOOK_SNAPSHOT,
            CanonicalEventType.BOOK_DELTA,
        }:
            continue
        engine.observe(event)
        try:
            snapshot = engine.snapshot(event.receive_timestamp_ns)
        except LookaheadError:  # pragma: no cover - ordering guard
            continue
    assert snapshot is not None
    json.dumps(snapshot.to_dict(), default=str)
