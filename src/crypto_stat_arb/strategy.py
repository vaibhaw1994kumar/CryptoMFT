from __future__ import annotations

from decimal import Decimal

from .fees import Liquidity
from .instruments import InstrumentMaster
from .models import Opportunity, Side, stable_id
from .orderbook import OrderBook
from .alignment import CrossVenueEligibilityPolicy


class CrossVenueSpreadStrategy:
    def __init__(
        self,
        strategy_id: str,
        instrument_id: str,
        venues: tuple[str, str],
        quantity: Decimal,
        entry_bps: Decimal,
        max_book_age_ns: int,
        min_net_edge_bps: Decimal,
        instruments: InstrumentMaster,
        eligibility_policy: CrossVenueEligibilityPolicy | None = None,
    ):
        self.strategy_id = strategy_id
        self.instrument_id = instrument_id
        self.venues = venues
        self.quantity = quantity
        self.entry_bps = entry_bps
        self.max_book_age_ns = max_book_age_ns
        self.min_net_edge_bps = min_net_edge_bps
        self.instruments = instruments
        self.eligibility_policy = eligibility_policy or CrossVenueEligibilityPolicy(
            venues, instrument_id, max_book_age_ns
        )
        self._seen_pairs: set[tuple[str, str]] = set()

    def evaluate(self, books: dict[tuple[str, str], OrderBook], now_ns: int) -> Opportunity | None:
        decision = self.eligibility_policy.evaluate(books, now_ns)
        if not decision.eligible:
            return None
        first = books[(self.venues[0], self.instrument_id)]
        second = books[(self.venues[1], self.instrument_id)]

        candidates = ((first, second), (second, first))
        buy_book, sell_book = max(
            candidates,
            key=lambda pair: (pair[1].best_bid - pair[0].best_ask) / pair[0].best_ask,
        )
        pair_key = (buy_book.last_event_id or "", sell_book.last_event_id or "")
        if pair_key in self._seen_pairs:
            return None
        gross_bps = (sell_book.best_bid - buy_book.best_ask) / buy_book.best_ask * Decimal("10000")
        # The dated, effective rate the ledger will book, not the instrument default.
        buy_fee = self.instruments.fee_bps(buy_book.venue, self.instrument_id, Liquidity.TAKER, now_ns)
        sell_fee = self.instruments.fee_bps(sell_book.venue, self.instrument_id, Liquidity.TAKER, now_ns)
        net_bps = gross_bps - buy_fee - sell_fee
        if gross_bps < self.entry_bps or net_bps < self.min_net_edge_bps:
            return None
        quantity = min(
            self.quantity,
            buy_book.available(side=Side.BUY, limit_price=buy_book.best_ask),
            sell_book.available(side=Side.SELL, limit_price=sell_book.best_bid),
        )
        quantity = min(
            self.instruments.round_quantity(buy_book.venue, self.instrument_id, quantity),
            self.instruments.round_quantity(sell_book.venue, self.instrument_id, quantity),
        )
        if quantity <= 0:
            return None
        self._seen_pairs.add(pair_key)
        opportunity_id = stable_id("opportunity", self.strategy_id, *pair_key)
        return Opportunity(
            opportunity_id=opportunity_id,
            strategy_id=self.strategy_id,
            instrument_id=self.instrument_id,
            observed_ts_ns=now_ns,
            buy_venue=buy_book.venue,
            sell_venue=sell_book.venue,
            buy_price=buy_book.best_ask,
            sell_price=sell_book.best_bid,
            quantity=quantity,
            gross_edge_bps=gross_bps,
            estimated_net_edge_bps=net_bps,
            source_event_ids=pair_key,
            eligibility_fingerprint=decision.fingerprint,
        )
