"""Regression coverage for defects found reviewing the state machine.

Each test here corresponds to a concrete failure that was reproduced
against the pre-fix code, so a regression re-breaks a named test rather
than silently degrading correctness.
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from trading_platform_poc.exchange_adapter import ExchangeAPIError
from trading_platform_poc.execution_controls import (
    ExecutionControlPlane,
    FeedMonitor,
    MessageBudget,
    MessageRequest,
    OrderStage,
    Priority,
)
from trading_platform_poc.models import OrderType, Side
from trading_platform_poc.opportunity import (
    LegRequest,
    LegState,
    MarketQuote,
    OpportunityCapacityError,
    OpportunityCoordinator,
    OpportunityRequest,
    OpportunityState,
    OpportunityTimeouts,
    ResidualPolicy,
    SignalHashMismatchError,
    _UNWIND_INDEX_BASE,
)
from trading_platform_poc.opportunity_store import (
    FillRecord,
    LegIdentityError,
    LegRecord,
    OpportunityStore,
)
from trading_platform_poc.simulated_venue import (
    FillStep,
    SimulatedVenue,
    VenueScript,
)

FAST = OpportunityTimeouts(
    ack_timeout_ms=50,
    first_fill_timeout_ms=200,
    complete_fill_timeout_ms=400,
    poll_interval_ms=1,
    max_submit_attempts=3,
    backoff_base_ms=1,
)


def _quotes(coordinator: OpportunityCoordinator) -> None:
    coordinator.update_quote(
        MarketQuote(
            venue="binance",
            symbol="BTCUSDT",
            bid_price=Decimal("99.9"),
            ask_price=Decimal("100.1"),
            fee_bps=Decimal("10"),
        )
    )
    coordinator.update_quote(
        MarketQuote(
            venue="okx",
            symbol="BTC-USDT",
            bid_price=Decimal("99.95"),
            ask_price=Decimal("100.05"),
            fee_bps=Decimal("2"),
        )
    )


def _request(
    *,
    signal_id: str = "sig-1",
    quantity: Decimal = Decimal("1"),
    sell_venue: str = "okx",
) -> OpportunityRequest:
    return OpportunityRequest(
        strategy_id="basis-arb",
        strategy_version="v1",
        signal_id=signal_id,
        canonical_symbol="BTC-USDT",
        expected_edge=Decimal("40"),
        legs=(
            LegRequest(
                venue="binance",
                symbol="BTCUSDT",
                side=Side.BUY,
                quantity=quantity,
                reference_price=Decimal("100"),
                order_type=OrderType.LIMIT,
                limit_price=Decimal("100"),
            ),
            LegRequest(
                venue=sell_venue,
                symbol="BTC-USDT",
                side=Side.SELL,
                quantity=quantity,
                reference_price=Decimal("101"),
                order_type=OrderType.LIMIT,
                limit_price=Decimal("101"),
            ),
        ),
    )


def _coordinator(
    adapters: dict[str, object],
    store: OpportunityStore,
    **kwargs: object,
) -> OpportunityCoordinator:
    coordinator = OpportunityCoordinator(
        adapters,
        store,
        timeouts=FAST,
        residual_policy=ResidualPolicy(),
        **kwargs,
    )
    _quotes(coordinator)
    return coordinator


def _leg_record(**overrides: object) -> LegRecord:
    base = {
        "opportunity_id": "OPP-1",
        "leg_index": 0,
        "venue": "binance",
        "symbol": "BTCUSDT",
        "side": Side.BUY.value,
        "order_type": OrderType.LIMIT.value,
        "target_quantity": Decimal("1"),
        "client_order_id": "cid-1",
        "idempotency_key": "idem-1",
        "state": LegState.SUBMITTING.value,
        "updated_ns": 1,
    }
    base.update(overrides)
    return LegRecord(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------
# #1 resume must resync before it resubmits
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_never_submits_a_second_order(tmp_path: Path) -> None:
    """A venue that frees client ids must not tempt us into duplicates.

    Binance releases a client order id once the order stops being live, so
    a blind resubmission during recovery opens a *second* real order. The
    coordinator has to query first. The opportunity is left non-terminal
    so resumption genuinely re-enters dispatch rather than short-circuiting
    on the stored result.
    """

    db = tmp_path / "resume.sqlite"
    binance = SimulatedVenue(
        "binance",
        script=VenueScript(
            fills=(FillStep(Decimal("1"), Decimal("100")),),
            allow_client_id_reuse=True,
        ),
    )
    okx = SimulatedVenue(
        "okx",
        script=VenueScript(
            fills=(FillStep(Decimal("1"), Decimal("101")),),
            allow_client_id_reuse=True,
        ),
    )
    adapters = {"binance": binance, "okx": okx}

    # Crash mid-flight: dispatch lands, then the process dies before fills
    # are awaited, leaving the opportunity non-terminal on disk.
    with OpportunityStore(db) as store:
        first = _coordinator(adapters, store)
        record = first._create(_request(), first.opportunity_id(_request()), [])
        record = first._reserve_risk(record, _request(), [])
        legs = first._build_legs(_request(), record.opportunity_id)
        first._create_orders(record, legs, [])
        await first._dispatch(record, legs, [])
        opportunity_id = record.opportunity_id

    with OpportunityStore(db) as store:
        assert not store.load_opportunity(opportunity_id).terminal
        # Drive the venue orders terminal so the client ids are reusable.
        for venue in (binance, okx):
            for order in await venue.open_orders():
                await venue.query_order(
                    order.symbol,
                    client_order_id=order.client_order_id,
                )

        second = _coordinator(adapters, store)
        dispatched: list[int] = []
        original = second._dispatch_leg

        async def counting_dispatch(  # type: ignore[no-untyped-def]
            oid: str, leg, strategy_id: str = ""
        ) -> None:
            dispatched.append(leg.index)
            await original(oid, leg, strategy_id)

        second._dispatch_leg = counting_dispatch  # type: ignore[assignment]
        await second.execute(_request())

    # Resumption really did re-enter dispatch...
    assert sorted(dispatched) == [0, 1]
    # ...and still placed exactly one order per leg.
    assert len(binance.submissions) == 1
    assert len(okx.submissions) == 1
    assert len(set(binance.submissions)) == len(binance.submissions)


@pytest.mark.asyncio
async def test_inconclusive_resync_never_resubmits(tmp_path: Path) -> None:
    """"We could not ask" is not the same as "the order is not there"."""

    class FlakyVenue(SimulatedVenue):
        async def query_order(self, symbol, **kwargs):  # type: ignore
            raise ExchangeAPIError(
                venue=self.venue,
                status=500,
                code=-1001,
                message="internal error",
                payload={},
            )

    venue = FlakyVenue(
        "binance",
        script=VenueScript(allow_client_id_reuse=True),
    )
    adapters = {"binance": venue, "okx": SimulatedVenue("okx")}
    with OpportunityStore(tmp_path / "flaky.sqlite") as store:
        coordinator = _coordinator(adapters, store)
        leg = coordinator._build_legs(_request(), "OPP-X")[0]
        # Pretend we already sent this leg before the process restarted.
        leg.submitted_ns = 1
        await coordinator._dispatch_leg("OPP-X", leg)

    assert venue.submissions == []
    assert leg.state is LegState.AMBIGUOUS


# ---------------------------------------------------------------------
# #2 unwind indexes and leg identity
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unwind_indexes_never_collide_after_restart(
    tmp_path: Path,
) -> None:
    """A resumed run must continue unwind numbering, not restart it.

    Unwind legs are not described by the request, so a resumed run that
    only rebuilt legs from the request would neither see their fills nor
    know their indexes were taken — re-minting index 1001 over a live one.
    """

    db = tmp_path / "unwind.sqlite"
    # One venue fills partially, the other fully, forcing a hedge.
    binance = SimulatedVenue(
        "binance",
        script=VenueScript(
            fills=(FillStep(Decimal("0.4"), Decimal("100")),),
            allow_client_id_reuse=True,
        ),
    )
    okx = SimulatedVenue(
        "okx",
        script=VenueScript(
            fills=(FillStep(Decimal("1"), Decimal("101")),),
            allow_client_id_reuse=True,
        ),
    )
    adapters = {"binance": binance, "okx": okx}

    with OpportunityStore(db) as store:
        coordinator = _coordinator(adapters, store)
        result = await coordinator.execute(_request())
        opportunity_id = result.opportunity_id
        unwound = [
            leg.leg_index
            for leg in store.load_legs(opportunity_id)
            if leg.leg_index >= _UNWIND_INDEX_BASE
        ]
        assert unwound, "scenario must actually unwind"
        store.save_opportunity(
            replace(
                store.load_opportunity(opportunity_id),
                terminal=False,
                state=OpportunityState.PARTIALLY_FILLED.value,
            )
        )

    submissions_before = len(okx.submissions) + len(binance.submissions)

    # Resume the very same signal from a cold coordinator.
    with OpportunityStore(db) as store:
        resumed = _coordinator(adapters, store)
        await resumed.execute(_request())
        indexes = [leg.leg_index for leg in store.load_legs(opportunity_id)]

    assert indexes == sorted(set(indexes)), "leg indexes collided"
    for venue in (binance, okx):
        assert len(venue.submissions) == len(set(venue.submissions))
    # Resumption must not manufacture a second hedge.
    assert (
        len(okx.submissions) + len(binance.submissions) == submissions_before
    )


def test_rewriting_a_leg_identity_is_refused(tmp_path: Path) -> None:
    with OpportunityStore(tmp_path / "identity.sqlite") as store:
        store.save_leg(_leg_record())

        # Same slot, different venue: this would book an okx trade under
        # binance's identity.
        with pytest.raises(LegIdentityError):
            store.save_leg(
                _leg_record(venue="okx", client_order_id="cid-2")
            )
        with pytest.raises(LegIdentityError):
            store.save_leg(_leg_record(side=Side.SELL.value))
        with pytest.raises(LegIdentityError):
            store.save_leg(_leg_record(target_quantity=Decimal("2")))

        # A repriced limit order is a legitimate update, not an identity
        # change, and must be persisted.
        store.save_leg(_leg_record(limit_price=Decimal("99")))
        assert store.load_legs("OPP-1")[0].limit_price == Decimal("99")


# ---------------------------------------------------------------------
# #3 a failed run must not leak its risk reservation
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancelled_execution_releases_the_reservation(
    tmp_path: Path,
) -> None:
    class HangingVenue(SimulatedVenue):
        async def submit_order(self, order):  # type: ignore[no-untyped-def]
            await asyncio.sleep(30)
            raise AssertionError("unreachable")

    adapters = {
        "binance": HangingVenue("binance"),
        "okx": HangingVenue("okx"),
    }
    with OpportunityStore(tmp_path / "leak.sqlite") as store:
        coordinator = _coordinator(adapters, store)
        task = asyncio.ensure_future(coordinator.execute(_request()))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert coordinator.reserved_notional == 0


# ---------------------------------------------------------------------
# #4 a failing dispatch must not orphan its sibling
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_dispatch_does_not_orphan_the_other_leg(
    tmp_path: Path,
) -> None:
    """A store failure on one leg must not leave its sibling running.

    ``_dispatch_leg`` persists its intent before calling the venue, so a
    store outage raises outside the classified-error path. Plain ``gather``
    would hand that exception to the caller while the other leg went on to
    place a real order milliseconds later.
    """

    finished: list[str] = []
    released = asyncio.Event()

    class SlowVenue(SimulatedVenue):
        async def submit_order(self, order):  # type: ignore[no-untyped-def]
            await released.wait()
            finished.append(self.venue)
            return await super().submit_order(order)

    adapters = {
        "binance": SlowVenue("binance", script=VenueScript()),
        "okx": SlowVenue("okx", script=VenueScript()),
    }
    with OpportunityStore(tmp_path / "orphan.sqlite") as store:
        original_save_leg = store.save_leg

        def failing_save_leg(record: LegRecord) -> None:
            if (
                record.venue == "binance"
                and record.state == LegState.SUBMITTING.value
            ):
                # Let the sibling reach the venue before we blow up.
                released.set()
                raise RuntimeError("store is down")
            original_save_leg(record)

        store.save_leg = failing_save_leg  # type: ignore[method-assign]

        coordinator = _coordinator(adapters, store)
        with pytest.raises(RuntimeError, match="store is down"):
            await coordinator.execute(_request())

        # By the time the failure surfaced, the sibling had already been
        # awaited to completion rather than left in flight.
        settled = list(finished)
        await asyncio.sleep(0.3)

    assert finished == settled, "a leg was still running after execute()"
    assert "binance" not in finished


# ---------------------------------------------------------------------
# #5 fees in different currencies must not be summed
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fees_are_reported_per_currency(tmp_path: Path) -> None:
    binance = SimulatedVenue(
        "binance",
        script=VenueScript(fills=(FillStep(Decimal("1"), Decimal("100")),)),
    )
    okx = SimulatedVenue(
        "okx",
        script=VenueScript(fills=(FillStep(Decimal("1"), Decimal("101")),)),
    )
    adapters = {"binance": binance, "okx": okx}

    with OpportunityStore(tmp_path / "fees.sqlite") as store:
        coordinator = _coordinator(adapters, store)
        result = await coordinator.execute(_request())
        opportunity_id = result.opportunity_id

        # A venue that charges in the base asset and signs the charge
        # negatively, as OKX does.
        store.record_fill(
            FillRecord(
                opportunity_id=opportunity_id,
                leg_index=0,
                venue="binance",
                trade_id="base-fee-1",
                exchange_order_id="X-1",
                side=Side.BUY.value,
                price=Decimal("100"),
                quantity=Decimal("0"),
                fee=Decimal("-0.001"),
                fee_currency="BTC",
                timestamp_ms=1,
            )
        )
        reconciliation = await coordinator.reconcile(opportunity_id)

    fees = reconciliation.fees_by_currency
    assert reconciliation.quote_currency == "USDT"
    # The base-asset charge is kept apart, not folded into the total.
    assert fees["BTC"] == Decimal("0.001")
    assert reconciliation.fees == fees["USDT"]
    assert reconciliation.fees > 0
    assert reconciliation.unconverted_fees == {"BTC": Decimal("0.001")}


@pytest.mark.asyncio
async def test_a_base_asset_fee_does_not_block_closure(
    tmp_path: Path,
) -> None:
    """A fee denomination is an accounting nuance, not unflat exposure.

    Binance charges spot BUY commission in the base asset. Treating that
    as a reconciliation failure would make the happy path terminal and
    strand the risk reservation forever.
    """

    class BaseAssetFeeVenue(SimulatedVenue):
        async def trades(self, symbol, **kwargs):  # type: ignore[no-untyped-def]
            found = await super().trades(symbol, **kwargs)
            return tuple(
                replace(trade, fee_currency="BTC") for trade in found
            )

    adapters = {
        "binance": BaseAssetFeeVenue(
            "binance",
            script=VenueScript(
                fills=(FillStep(Decimal("1"), Decimal("100")),)
            ),
        ),
        "okx": SimulatedVenue(
            "okx",
            script=VenueScript(
                fills=(FillStep(Decimal("1"), Decimal("101")),)
            ),
        ),
    }
    with OpportunityStore(tmp_path / "basefee.sqlite") as store:
        coordinator = _coordinator(adapters, store)
        result = await coordinator.execute(_request())

    assert result.state is OpportunityState.CLOSED
    assert result.reconciliation is not None
    assert result.reconciliation.risk_released
    assert result.reconciliation.unconverted_fees.get("BTC")
    assert coordinator.reserved_notional == 0


@pytest.mark.asyncio
async def test_risk_is_released_even_when_reconciliation_fails(
    tmp_path: Path,
) -> None:
    """A terminal opportunity is never revisited, so it must not hold risk."""

    adapters = {
        "binance": SimulatedVenue(
            "binance",
            script=VenueScript(
                fills=(FillStep(Decimal("1"), Decimal("100")),)
            ),
        ),
        "okx": SimulatedVenue("okx", script=VenueScript()),
    }
    with OpportunityStore(tmp_path / "unbalanced.sqlite") as store:
        coordinator = _coordinator(adapters, store)
        # Force reconciliation to report an unflat book so closure takes
        # the RECONCILIATION_REQUIRED branch.
        original_reconcile = coordinator.reconcile

        async def unbalanced(opportunity_id, legs=None):  # type: ignore
            report = await original_reconcile(opportunity_id, legs)
            return replace(
                report,
                discrepancies=("injected imbalance",),
            )

        coordinator.reconcile = unbalanced  # type: ignore[assignment]
        result = await coordinator.execute(_request())

        assert result.state is OpportunityState.RECONCILIATION_REQUIRED
        assert coordinator.reserved_notional == 0
        assert result.reconciliation is not None
        assert result.reconciliation.risk_released


def test_quote_currency_is_derived_from_the_symbol() -> None:
    derive = OpportunityCoordinator._quote_currency
    assert derive("BTC-USDT") == "USDT"
    assert derive("BTC/USDC") == "USDC"
    assert derive("BTCUSDT") == "USDT"
    assert derive("ETHBTC") == "BTC"
    assert derive("") == "USDT"


# ---------------------------------------------------------------------
# #6 reference price must survive a restart
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slippage_is_stable_across_a_restart(tmp_path: Path) -> None:
    db = tmp_path / "slippage.sqlite"
    adapters = {
        "binance": SimulatedVenue(
            "binance",
            script=VenueScript(
                fills=(FillStep(Decimal("1"), Decimal("107")),)
            ),
        ),
        "okx": SimulatedVenue(
            "okx",
            script=VenueScript(
                fills=(FillStep(Decimal("1"), Decimal("101")),)
            ),
        ),
    }

    with OpportunityStore(db) as store:
        coordinator = _coordinator(adapters, store)
        result = await coordinator.execute(_request())
        assert result.reconciliation is not None
        in_process = result.reconciliation.slippage
        opportunity_id = result.opportunity_id

    # A brand new process reconciling from the ledger alone.
    with OpportunityStore(db) as store:
        restarted = _coordinator(adapters, store)
        after_restart = await restarted.reconcile(opportunity_id)

    assert after_restart.slippage == in_process


def test_reference_price_round_trips_through_the_store(
    tmp_path: Path,
) -> None:
    with OpportunityStore(tmp_path / "ref.sqlite") as store:
        store.save_leg(
            _leg_record(
                reference_price=Decimal("100.25"),
                limit_price=Decimal("99"),
            )
        )
        restored = store.load_legs("OPP-1")[0]

    assert restored.reference_price == Decimal("100.25")
    assert restored.limit_price == Decimal("99")


def test_a_ledger_without_the_new_column_is_migrated(
    tmp_path: Path,
) -> None:
    """Upgrading the code must not strand an in-flight ledger.

    ``CREATE TABLE IF NOT EXISTS`` is a no-op on an existing database, so
    without a migration every read and write would fail exactly when it
    matters most: during restart recovery.
    """

    db = tmp_path / "legacy.sqlite"
    with OpportunityStore(db) as store:
        store.save_leg(_leg_record(reference_price=Decimal("100")))

    # Simulate a ledger written before the column existed.
    connection = sqlite3.connect(db)
    connection.execute("ALTER TABLE legs DROP COLUMN reference_price")
    connection.commit()
    connection.close()

    with OpportunityStore(db) as store:
        restored = store.load_legs("OPP-1")[0]
        assert restored.reference_price is None
        # And writing still works against the migrated table.
        store.save_leg(
            _leg_record(reference_price=Decimal("101"), attempt=1)
        )
        assert store.load_legs("OPP-1")[0].reference_price == Decimal("101")


# ---------------------------------------------------------------------
# #7 the stored signal hash must gate resumption
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resuming_a_different_signal_is_refused(
    tmp_path: Path,
) -> None:
    db = tmp_path / "hash.sqlite"
    adapters = {
        "binance": SimulatedVenue("binance", script=VenueScript()),
        "okx": SimulatedVenue("okx", script=VenueScript()),
        "kraken": SimulatedVenue("kraken", script=VenueScript()),
    }
    original = _request()
    # Same strategy, version and signal id, so the same opportunity id,
    # but a materially different plan.
    impostor = _request(sell_venue="kraken")

    with OpportunityStore(db) as store:
        coordinator = _coordinator(adapters, store)
        await coordinator.execute(original)

    with OpportunityStore(db) as store:
        coordinator = _coordinator(adapters, store)
        assert coordinator.opportunity_id(
            impostor
        ) == coordinator.opportunity_id(original)
        with pytest.raises(SignalHashMismatchError):
            await coordinator.execute(impostor)


def test_signal_hash_covers_price_and_order_type(tmp_path: Path) -> None:
    """Two plans that differ only in price must not resume each other."""

    with OpportunityStore(tmp_path / "hashcover.sqlite") as store:
        coordinator = _coordinator(
            {"binance": SimulatedVenue("binance"), "okx": SimulatedVenue("okx")},
            store,
        )
        base = _request()
        repriced = replace(
            base,
            legs=(
                replace(base.legs[0], limit_price=Decimal("99")),
                base.legs[1],
            ),
        )
        market = replace(
            base,
            legs=(
                replace(
                    base.legs[0],
                    order_type=OrderType.MARKET,
                    limit_price=None,
                ),
                base.legs[1],
            ),
        )

        assert coordinator.signal_hash(base) != coordinator.signal_hash(
            repriced
        )
        assert coordinator.signal_hash(base) != coordinator.signal_hash(
            market
        )


# ---------------------------------------------------------------------
# #8 the risk view must be independent of the leg table
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_risk_view_is_not_derived_from_the_leg_table(
    tmp_path: Path,
) -> None:
    adapters = {
        "binance": SimulatedVenue(
            "binance",
            script=VenueScript(
                fills=(FillStep(Decimal("1"), Decimal("100")),)
            ),
        ),
        "okx": SimulatedVenue(
            "okx",
            script=VenueScript(
                fills=(FillStep(Decimal("1"), Decimal("101")),)
            ),
        ),
    }

    with OpportunityStore(tmp_path / "risk.sqlite") as store:
        coordinator = _coordinator(adapters, store)
        result = await coordinator.execute(_request())
        opportunity_id = result.opportunity_id

        assert result.reconciliation is not None
        assert result.reconciliation.balanced
        assert result.reconciliation.risk_positions == {
            "binance": Decimal("1"),
            "okx": Decimal("-1"),
        }

        # Corrupt the ledger only. A risk view recomputed from these rows
        # could never notice; an independent one must.
        legs = store.load_legs(opportunity_id)
        store.save_leg(
            replace(legs[0], filled_quantity=Decimal("5"))
        )
        drifted = await coordinator.reconcile(opportunity_id)

    assert not drifted.balanced
    assert any(
        "risk" in discrepancy and "local" in discrepancy
        for discrepancy in drifted.discrepancies
    )


@pytest.mark.asyncio
async def test_closed_opportunity_reports_every_view(tmp_path: Path) -> None:
    adapters = {
        "binance": SimulatedVenue(
            "binance",
            script=VenueScript(
                fills=(FillStep(Decimal("1"), Decimal("100")),)
            ),
        ),
        "okx": SimulatedVenue(
            "okx",
            script=VenueScript(
                fills=(FillStep(Decimal("1"), Decimal("101")),)
            ),
        ),
    }
    with OpportunityStore(tmp_path / "views.sqlite") as store:
        coordinator = _coordinator(adapters, store)
        result = await coordinator.execute(_request())

    assert result.state is OpportunityState.CLOSED
    payload = result.reconciliation.to_dict()  # type: ignore[union-attr]
    for key in (
        "exchange_positions",
        "local_positions",
        "risk_positions",
        "ledger_position",
        "fees_by_currency",
        "quote_currency",
    ):
        assert key in payload


# ---------------------------------------------------------------------
# Opportunity <-> execution control plane integration
#
# Every test above runs with ``control_plane=None``. The control plane
# was wired in later, so without this section the entire integration is
# exercised by nothing at all.
# ---------------------------------------------------------------------


def _plane(
    *,
    binance: int = 1000,
    okx: int = 1000,
    firm: int = 1000,
    stale_after_ms: int = 10**9,
    tick: bool = True,
) -> ExecutionControlPlane:
    plane = ExecutionControlPlane(
        venue_budgets={
            "binance": MessageBudget(
                name="binance", limit=binance, window_ms=100_000
            ),
            "okx": MessageBudget(name="okx", limit=okx, window_ms=100_000),
        },
        global_budget=MessageBudget(
            name="firm", limit=firm, window_ms=100_000
        ),
        feed_monitor=FeedMonitor(stale_after_ms=stale_after_ms),
    )
    if tick:
        for venue in ("binance", "okx"):
            plane.feed_monitor.record_tick(venue)
    return plane


def _filling_adapters() -> dict[str, SimulatedVenue]:
    return {
        "binance": SimulatedVenue(
            "binance",
            script=VenueScript(
                fills=(FillStep(Decimal("1"), Decimal("100")),)
            ),
        ),
        "okx": SimulatedVenue(
            "okx",
            script=VenueScript(
                fills=(FillStep(Decimal("1"), Decimal("101")),)
            ),
        ),
    }


@pytest.mark.asyncio
async def test_refused_message_budget_transmits_nothing(
    tmp_path: Path,
) -> None:
    """Capacity is reserved for *all* legs before the first one is sent.

    Reserving leg-by-leg would let leg A execute and leg B discover there
    is no budget left, which is a naked position nobody asked for. The
    refusal must therefore cost the whole opportunity and nothing else.
    """

    adapters = _filling_adapters()
    store = OpportunityStore(tmp_path / "cap.sqlite")
    # Three messages per leg are planned; okx can fund only two.
    plane = _plane(okx=2)
    coordinator = _coordinator(adapters, store, control_plane=plane)

    with pytest.raises(OpportunityCapacityError):
        await coordinator.execute(_request())

    assert adapters["binance"].submissions == []
    assert adapters["okx"].submissions == []
    # A reservation left dangling would wedge every later opportunity.
    assert coordinator.reserved_notional == Decimal(0)
    assert plane.venue_budgets["binance"].snapshot().reserved == 0
    assert plane.venue_budgets["okx"].snapshot().reserved == 0


@pytest.mark.asyncio
async def test_kill_switch_stops_legs_before_they_reach_a_venue(
    tmp_path: Path,
) -> None:
    """Capacity exists, but the kill switch is absolute."""

    adapters = _filling_adapters()
    store = OpportunityStore(tmp_path / "kill.sqlite")
    plane = _plane()
    coordinator = _coordinator(adapters, store, control_plane=plane)
    plane.kill_switch.engage(actor="ops", reason="drill")

    result = await coordinator.execute(_request())

    assert adapters["binance"].submissions == []
    assert adapters["okx"].submissions == []
    states = {leg.state for leg in result.legs}
    assert states == {LegState.BLOCKED}
    # Closing is correct -- nothing transmitted means nothing to unwind --
    # but it must close *flat*, having never reached a fill state.
    assert all(leg.filled_quantity == Decimal(0) for leg in result.legs)
    assert "acknowledged" not in result.transitions
    assert "partially_filled" not in result.transitions


@pytest.mark.asyncio
async def test_a_stale_feed_stops_legs_before_they_reach_a_venue(
    tmp_path: Path,
) -> None:
    """Quoting on a feed we cannot trust is how firms lose money."""

    adapters = _filling_adapters()
    store = OpportunityStore(tmp_path / "stale.sqlite")
    plane = _plane(stale_after_ms=1, tick=False)
    coordinator = _coordinator(adapters, store, control_plane=plane)

    result = await coordinator.execute(_request())

    assert adapters["binance"].submissions == []
    assert adapters["okx"].submissions == []
    assert {leg.state for leg in result.legs} == {LegState.BLOCKED}


@pytest.mark.asyncio
async def test_an_unwind_leg_is_never_blocked_by_a_saturated_control(
    tmp_path: Path,
) -> None:
    """The highest-value guarantee in the whole integration.

    A residual position is exposure we already hold. Being throttled out
    of closing it turns a capacity problem into a risk problem, so
    unwinds are presented as emergency hedges and are exempt from every
    capacity control. Here leg B rejects, leaving leg A's fill exposed,
    and *then* -- at the exact moment the first unwind is offered -- every
    budget is exhausted and both feeds go stale. The unwind must still
    reach the venue and flatten the book.
    """

    adapters = {
        "binance": SimulatedVenue(
            "binance",
            script=VenueScript(
                fills=(FillStep(Decimal("1"), Decimal("100")),)
            ),
        ),
        "okx": SimulatedVenue(
            "okx", script=VenueScript(reject_message="price band")
        ),
    }
    store = OpportunityStore(tmp_path / "unwind.sqlite")
    plane = _plane()
    coordinator = _coordinator(adapters, store, control_plane=plane)

    original_admit = coordinator._admit_leg
    offered: list[int] = []

    def admit(opportunity_id: str, strategy_id: str, leg: object) -> bool:
        if getattr(leg, "is_unwind", False):
            offered.append(getattr(leg, "index"))
            if len(offered) == 1:
                # Exhaust every capacity control and blind both feeds at
                # the worst possible moment: we are long and unhedged.
                plane.global_budget.force_consume(10**6)
                for budget in plane.venue_budgets.values():
                    budget.force_consume(10**6)
                plane.feed_monitor._last_tick_ns.clear()
                # The venue that rejected the hedge now accepts, so the
                # only thing that could still stop us is our own throttle.
                adapters["okx"].script = VenueScript(
                    fills=(FillStep(Decimal("1"), Decimal("101")),)
                )
        return original_admit(opportunity_id, strategy_id, leg)

    coordinator._admit_leg = admit  # type: ignore[assignment]

    result = await coordinator.execute(_request())

    assert offered, "no unwind leg was ever offered"
    # Every ordinary message would now be refused outright.
    blocked = plane.evaluate(
        MessageRequest(
            message_id="probe",
            strategy_id="basis-arb",
            venue="okx",
            stage=OrderStage.NEW,
            priority=Priority.MARKET_MAKING,
        )
    )
    assert not blocked.transmits

    unwinds = [
        leg for leg in result.legs if leg.leg_index >= _UNWIND_INDEX_BASE
    ]
    assert unwinds, "expected a deterministic unwind"
    assert all(leg.state is not LegState.BLOCKED for leg in unwinds)
    assert adapters["okx"].submissions, "the unwind never reached a venue"
    assert sum(leg.filled_quantity for leg in unwinds) == Decimal("1")
    assert result.reconciliation.net_position == Decimal(0)
    assert result.reconciliation.discrepancies == ()


@pytest.mark.asyncio
async def test_capacity_is_released_when_the_opportunity_closes(
    tmp_path: Path,
) -> None:
    """A reservation that outlives its opportunity starves the next."""

    adapters = _filling_adapters()
    store = OpportunityStore(tmp_path / "release.sqlite")
    plane = _plane()
    coordinator = _coordinator(adapters, store, control_plane=plane)

    result = await coordinator.execute(_request())

    assert result.state is OpportunityState.CLOSED
    for budget in (*plane.venue_budgets.values(), plane.global_budget):
        assert budget.snapshot().reserved == 0
    # Consumed capacity is *not* released -- the messages really were sent.
    assert plane.venue_budgets["binance"].snapshot().used > 0


@pytest.mark.asyncio
async def test_absent_control_plane_leaves_behaviour_unchanged(
    tmp_path: Path,
) -> None:
    """The plane is opt-in; ``None`` must be the pre-integration path."""

    without = _filling_adapters()
    result_without = await _coordinator(
        without, OpportunityStore(tmp_path / "a.sqlite")
    ).execute(_request())

    with_plane = _filling_adapters()
    result_with = await _coordinator(
        with_plane,
        OpportunityStore(tmp_path / "b.sqlite"),
        control_plane=_plane(),
    ).execute(_request())

    assert result_without.state is result_with.state is OpportunityState.CLOSED
    assert len(without["binance"].submissions) == len(
        with_plane["binance"].submissions
    )
    assert result_without.reconciliation == result_with.reconciliation


@pytest.mark.asyncio
async def test_an_opportunity_spends_only_what_it_reserved(
    tmp_path: Path,
) -> None:
    """Reserving N and sending N must cost N, not 2N.

    The coordinator reserves message capacity for every planned leg
    before the first one transmits. If dispatch then took capacity again
    the firm would run out at half its stated limit, and the reservation
    -- which counts against availability -- could be what throttles the
    very legs it was taken for.
    """

    adapters = _filling_adapters()
    store = OpportunityStore(tmp_path / "spend.sqlite")
    # Exactly enough for the planned messages and not one more.
    plane = _plane(binance=3, okx=3, firm=6)
    coordinator = _coordinator(
        adapters, store, control_plane=plane, messages_per_leg=3,
        hedge_messages=0,
    )

    result = await coordinator.execute(_request())

    assert result.state is OpportunityState.CLOSED
    assert adapters["binance"].submissions
    assert adapters["okx"].submissions
    for budget in (*plane.venue_budgets.values(), plane.global_budget):
        snapshot = budget.snapshot()
        assert snapshot.reserved == 0, budget.name
        assert snapshot.used <= snapshot.limit, budget.name


def test_two_legs_on_one_venue_reserve_for_both(tmp_path: Path) -> None:
    """The coordinator must fold venue names exactly as the ledger does.

    The coordinator accumulated under ``.lower()`` while the ledger
    folds with ``strip().casefold()``, so two legs on one venue whose
    strings differed only by whitespace stayed two keys here and
    collapsed to one there -- reserving one leg's messages for two. The
    second leg then found no reserved capacity, which under
    ``require_reservation`` is a hard reject of a leg whose partner is
    already on the wire: the naked-leg outcome atomic reservation
    exists to prevent.
    """

    from trading_platform_poc.opportunity import _Leg, OpportunityRecord

    store = OpportunityStore(tmp_path / "fold.sqlite")
    plane = _plane(binance=10, okx=10, firm=20)
    coordinator = _coordinator(
        _filling_adapters(), store, control_plane=plane,
        messages_per_leg=3, hedge_messages=0,
    )
    request = _request()
    record = OpportunityRecord(
        opportunity_id="OPP-FOLD",
        strategy_id=request.strategy_id,
        strategy_version=request.strategy_version,
        signal_hash="hash",
        canonical_symbol="BTC-USDT",
        target_quantity=Decimal("1"),
        expected_edge=request.expected_edge,
        state=OpportunityState.CREATED.value,
        created_ns=0,
        updated_ns=0,
        metadata={},
    )
    store.save_opportunity(record)

    def leg(index: int, venue: str) -> _Leg:
        return _Leg(
            index=index,
            venue=venue,
            symbol="BTCUSDT",
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            target_quantity=Decimal("1"),
            reference_price=Decimal("100"),
            limit_price=Decimal("100"),
            source_id=f"src-{index}",
            client_order_id=f"coid-{index}",
            idempotency_key=f"idem-{index}",
        )

    coordinator._reserve_capacity(
        record, request, [leg(0, "binance"), leg(1, "Binance ")]
    )

    held = plane.ledger.get("OPP-FOLD")
    assert held is not None
    assert dict(held.venue_messages) == {"binance": 6}
    assert plane.venue_budgets["binance"].snapshot().reserved == 6


@pytest.mark.asyncio
async def test_a_malformed_reservation_is_translated_not_leaked(
    tmp_path: Path,
) -> None:
    """The request is built inside the `try`, so its errors belong to it.

    `ReservationRequest.__post_init__` raises `ValueError` for a
    malformed request. Catching only `CapacityReservationError` let that
    escape past the `capacity_refused` audit event, past the structured
    log and past the translation, surfacing untranslated to a caller
    that had every reason to expect an opportunity-level error.

    Constructor validation makes this unreachable from a well-formed
    coordinator, so the test reaches it the way a future code path
    would: by setting the field after construction.
    """

    adapters = _filling_adapters()
    store = OpportunityStore(tmp_path / "malformed.sqlite")
    coordinator = _coordinator(adapters, store, control_plane=_plane())
    coordinator.hedge_messages = -1

    with pytest.raises(OpportunityCapacityError):
        await coordinator.execute(_request())

    assert adapters["binance"].submissions == []
    assert adapters["okx"].submissions == []
    assert coordinator.reserved_notional == Decimal(0)
