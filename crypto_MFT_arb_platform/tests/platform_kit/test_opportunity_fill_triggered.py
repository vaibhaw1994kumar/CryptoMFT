"""Method A in the live opportunity engine: DispatchMode.FILL_TRIGGERED (docs/strategy_contract.md section 3).

The maker leg is dispatched alone; the hedge legs are dispatched only after the maker fill is confirmed, sized to the confirmed
filled quantity (partial fills included); if the maker never fills no hedge order is ever submitted. CONCURRENT is unchanged.
"""
from __future__ import annotations

import time
from decimal import Decimal

import pytest

from trading_platform_poc.models import Order, OrderType, Side
from trading_platform_poc.opportunity import DispatchMode, LegRequest, LegState, OpportunityRequest, OpportunityTimeouts
from trading_platform_poc.opportunity_store import OpportunityStore

from test_opportunity import FAST, ScriptedAdapter, build_coordinator


class Recording(ScriptedAdapter):
    """Records every submit with the OTHER venue's filled quantity at that instant, so sequencing can be asserted."""
    log: list = []
    peers: dict = {}

    async def submit_order(self, order: Order):
        peer = self.peers.get(self.venue)
        peer_filled = None
        if peer is not None:
            peer_filled = sum((o.filled_quantity for o in peer.orders.values()), Decimal(0))
        Recording.log.append(("submit", self.venue, time.monotonic_ns(), peer_filled, order.quantity))
        return await super().submit_order(order)


def request(quantity=Decimal("10"), mode=DispatchMode.FILL_TRIGGERED, maker=(True, False), signal_id="sig-ft") -> OpportunityRequest:
    return OpportunityRequest(
        strategy_id="perp-perp-maker-taker", strategy_version="v1", signal_id=signal_id, canonical_symbol="BTC-USDT", expected_edge=Decimal("3"),
        dispatch_mode=mode,
        legs=(LegRequest(venue="binance", symbol="BTCUSDT", side=Side.BUY, quantity=quantity, reference_price=Decimal("100"), order_type=OrderType.LIMIT,
                         limit_price=Decimal("100"), maker=maker[0]),
              LegRequest(venue="okx", symbol="BTC-USDT", side=Side.SELL, quantity=quantity, reference_price=Decimal("101"), order_type=OrderType.LIMIT,
                         limit_price=Decimal("101"), maker=maker[1])))


def adapters(maker_schedule):
    Recording.log = []
    binance = Recording("binance", fill_schedule=maker_schedule)
    okx = Recording("okx")
    Recording.peers = {"binance": okx, "okx": binance}
    return {"binance": binance, "okx": okx}


@pytest.mark.asyncio
async def test_hedge_is_submitted_only_after_the_maker_fill_and_for_the_filled_quantity():
    ad = adapters((Decimal(0), Decimal(0), Decimal("10")))               # acked unfilled, still unfilled on the first poll, filled on the second
    with OpportunityStore() as store:
        c = build_coordinator(ad, store)
        result = await c.execute(request())
    submits = [x for x in Recording.log if x[0] == "submit"]
    assert [s[1] for s in submits] == ["binance", "okx"]                 # maker first, hedge second, never together
    assert submits[1][3] == Decimal("10")                                # when okx was submitted, binance was already filled in full
    assert submits[1][4] == Decimal("10")
    assert submits[1][2] > submits[0][2]
    assert result.dispatch_mode == "fill_triggered"
    assert result.residual_quantity == 0
    assert [leg.state for leg in result.legs] == [LegState.FILLED, LegState.FILLED]
    lg = result.legging
    assert lg["maker_filled"] == "10" and lg["hedge_dispatch_ns"] >= lg["maker_first_fill_ns"] and lg["legging_ns"] >= 0
    assert "hedge_dispatched:10" in result.transitions
    assert result.state.value == "closed"


@pytest.mark.asyncio
async def test_maker_never_fills_no_hedge_order_is_ever_sent():
    ad = adapters((Decimal(0),))                                          # acked, then never fills: cancelled at the fill deadline
    with OpportunityStore() as store:
        c = build_coordinator(ad, store, timeouts=OpportunityTimeouts(ack_timeout_ms=50, first_fill_timeout_ms=60, complete_fill_timeout_ms=120,
                                                                     poll_interval_ms=1, max_submit_attempts=3, backoff_base_ms=1))
        result = await c.execute(request())
    assert ad["okx"].submitted == []                                      # the hedge venue never saw an order
    assert ad["binance"].canceled                                         # the maker was cancelled at its deadline
    maker, hedge = result.legs
    assert maker.filled_quantity == 0 and maker.state in (LegState.CANCELED, LegState.TIMED_OUT)
    assert hedge.state == LegState.BLOCKED and "maker never filled" in (hedge.last_error or "")
    assert result.residual_quantity == 0 and result.unwinds == ()
    assert "hedge_not_sent:maker_unfilled" in result.transitions
    assert result.legging["maker_filled"] == "0" and result.legging["hedge_dispatch_ns"] is None


@pytest.mark.asyncio
async def test_partial_maker_fill_hedges_exactly_the_filled_quantity():
    ad = adapters((Decimal("3"), Decimal("3")))                           # 3 of 10 fills, then nothing more until the deadline
    with OpportunityStore() as store:
        c = build_coordinator(ad, store, timeouts=OpportunityTimeouts(ack_timeout_ms=50, first_fill_timeout_ms=60, complete_fill_timeout_ms=120,
                                                                     poll_interval_ms=1, max_submit_attempts=3, backoff_base_ms=1))
        result = await c.execute(request())
    okx_orders = ad["okx"].submitted
    assert [o.quantity for o in okx_orders] == [Decimal("3")]            # the hedge is sized to the confirmed fill, not the request
    maker, hedge = result.legs
    assert (maker.filled_quantity, hedge.target_quantity, hedge.filled_quantity) == (Decimal("3"), Decimal("3"), Decimal("3"))
    assert result.residual_quantity == 0 and result.unwinds == ()
    assert result.legging["maker_filled"] == "3"


@pytest.mark.asyncio
async def test_concurrent_mode_is_unchanged_and_has_no_legging_block():
    ad = adapters(None)
    with OpportunityStore() as store:
        c = build_coordinator(ad, store)
        result = await c.execute(request(mode=DispatchMode.CONCURRENT, maker=(False, False), signal_id="sig-cc"))
    assert result.dispatch_mode == "concurrent" and result.legging is None
    assert {s[1] for s in Recording.log if s[0] == "submit"} == {"binance", "okx"}
    assert result.residual_quantity == 0


def test_request_validation_makes_the_distinction_explicit():
    with pytest.raises(ValueError):
        request(mode=DispatchMode.CONCURRENT, maker=(True, False))        # a maker leg under concurrent dispatch is Method B: refused
    with pytest.raises(ValueError):
        request(mode=DispatchMode.FILL_TRIGGERED, maker=(False, False))   # fill-triggered needs a maker leg
    with pytest.raises(ValueError):
        request(mode=DispatchMode.FILL_TRIGGERED, maker=(True, True))     # and exactly one
    request(mode=DispatchMode.FILL_TRIGGERED, maker=(True, False))        # the contract shape
