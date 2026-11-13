from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .fees import Liquidity
from .instruments import InstrumentMaster
from .models import Opportunity, Side, stable_id
from .orderbook import OrderBook
from .alignment import CrossVenueEligibilityPolicy
from .strategies.edge import cross_gross_bps


# ---- the market-making quote interface (owner 13 Nov, CR-MQ part 3) --------------------------------------------------------------------------------------------------------------------------------------
# A strategy that sets `quote_book = True` is called by TradingSession.on_event (replay, live paper and live share that one call site) after the timers have run and the book is applied:
#     strategy.quotes(books, now_ns) -> list of actions: PlaceQuote | AmendQuote | CancelQuote | ExitInventory
# and receives, keyed by the quote id it chose (each is optional on the strategy):
#     on_quote_ack(quote_id, accepted, reason, ts_ns)
#     on_quote_fill(quote_id, qty, price, fee, liquidity, ts_ns)          each unique execution once (the ledger's de-duplicated fills)
#     on_quote_closed(quote_id, reason, ts_ns)                            filled / cancelled / rejected / amend_refused:<why> / blocked / rate_limit_skipped, with the reason
#     on_quote_amend_refused(quote_id, reason, ts_ns)                     a refused amend of a quote that is still live
# Off unless the strategy opts in with the exact attribute `quote_book = True`: an arb strategy is never called this way and every arb result is unchanged.
@dataclass(frozen=True)
class PlaceQuote:
    quote_id: str
    venue: str
    side: str                 # "buy" | "sell"
    price: Decimal
    qty: Decimal


@dataclass(frozen=True)
class AmendQuote:
    quote_id: str
    price: Decimal
    qty: Decimal | None = None   # the OMS amend keeps the quote's quantity (the REPLACE carries the quote total); a different qty is refused


@dataclass(frozen=True)
class CancelQuote:
    quote_id: str


@dataclass(frozen=True)
class ExitInventory:
    quote_id: str             # the id the callbacks use for this exit
    venue: str
    side: str
    qty: Decimal
    cap_price: Decimal        # the worst price (an IOC limit): a buy never above it, a sell never below it


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
            key=lambda pair: cross_gross_bps(pair[0].best_ask, pair[1].best_bid),
        )
        pair_key = (buy_book.last_event_id or "", sell_book.last_event_id or "")
        if pair_key in self._seen_pairs:
            return None
        gross_bps = cross_gross_bps(buy_book.best_ask, sell_book.best_bid)
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
