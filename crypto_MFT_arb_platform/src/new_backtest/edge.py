"""The one place an arb edge is computed (docs/strategy_contract.md, section 2).

Two profiles:

* ``perp_perp_maker_taker_v1``
  - ``taker_taker_edge_bps``: depth-weighted. The buy VWAP walks the buy venue's asks and the sell VWAP walks the sell venue's
    bids over the levels ``size`` consumes; ``insufficient_depth`` when either book cannot fill the size. Reference price
    (``ref``) = the BUY venue's mid at decision time, so a bps figure means "per unit of the price we are about to pay" and the
    two directions (buy A / sell B, buy B / sell A) are two explicit calls with their own reference, never a sign flip.
  - ``maker_taker_expected_edge_bps``: conditional. The hedge is not executed at the quote it shows now; it is executed after
    the maker fill, at the price the hedge venue has moved to by then. The expected hedge price is the hedge touch now moved by
    ``expected_hedge_move_bps`` (estimated from replay: the hedge venue's move between quote time and fill time, conditional on a
    fill, by side; NEGATIVE = adverse), less expected slippage beyond the touch, less funding/holding, less p_fail x loss_fail.
    The naive edge (current opposite quote locked) is returned alongside so the gap is visible on every signal.

* ``legacy_v0``: ``legacy_taker_taker_edge_bps`` / ``legacy_maker_bid_edge_bps`` / ``legacy_maker_ask_edge_bps`` reproduce the
  expressions used by every result before 2026-11-01 exactly (top of book, reference = buy ask / our maker price / hedge price).

All functions are pure. ``fees_bps`` is the sum of the two legs' fee rates in bps (a maker rebate is negative).
"""
from __future__ import annotations

from dataclasses import dataclass

INSUFFICIENT_DEPTH = "insufficient_depth"
NO_BOOK = "no_book"
OK = "ok"


@dataclass(frozen=True)
class EdgeResult:
    status: str                      # ok | insufficient_depth | no_book
    gross_bps: float | None          # before fees, relative to ref
    fee_bps: float
    net_bps: float | None            # gross - fees (the caller subtracts its own safety buffer)
    buy_vwap: float | None
    sell_vwap: float | None
    ref: float | None
    ref_name: str
    size: float
    buy_depth: float = 0.0           # depth available on the walked levels (for diagnostics)
    sell_depth: float = 0.0
    naive_bps: float | None = None   # maker/taker only: the edge with the current opposite quote locked, before fees
    components: dict | None = None   # maker/taker only: each deduction in bps


def vwap_for_size(book, side: str, size: float) -> tuple[float | None, float]:
    """(VWAP over the levels `size` consumes on `side` ("bid" | "ask"), depth walked). VWAP is None when the book cannot fill
    the size. Walks best-first; a level is consumed in full until the last one, which is consumed partially."""
    if size <= 0 or book is None or not getattr(book, "ready", False):
        return None, 0.0
    is_bid = side.startswith("b")
    prices = book._sorted_bids() if is_bid else book._sorted_asks()
    levels = book._bids if is_bid else book._asks
    left, cost, walked = size, 0.0, 0.0
    for p in prices:
        q = levels.get(p, 0.0)
        if q <= 0:
            continue
        take = min(q, left)
        cost += take * p
        walked += take
        left -= take
        if left <= 1e-12:
            return cost / size, walked
    return None, walked


def taker_taker_edge_bps(buy_book, sell_book, size: float, buy_fee_bps: float, sell_fee_bps: float, ref: float | None = None) -> EdgeResult:
    """Depth-weighted taker/taker edge: 1e4 x (vwap_bid_sell(size) - vwap_ask_buy(size)) / ref - fees.
    ref defaults to the BUY venue's mid at decision time (ref_name "buy_mid")."""
    fee = buy_fee_bps + sell_fee_bps
    if buy_book is None or sell_book is None or not buy_book.ready or not sell_book.ready:
        return EdgeResult(NO_BOOK, None, fee, None, None, None, None, "buy_mid", size)
    r = ref if ref is not None else buy_book.mid
    if r is None or r <= 0:
        return EdgeResult(NO_BOOK, None, fee, None, None, None, None, "buy_mid", size)
    buy_vwap, bd = vwap_for_size(buy_book, "ask", size)
    sell_vwap, sd = vwap_for_size(sell_book, "bid", size)
    if buy_vwap is None or sell_vwap is None:
        return EdgeResult(INSUFFICIENT_DEPTH, None, fee, None, buy_vwap, sell_vwap, r, "buy_mid", size, bd, sd)
    gross = (sell_vwap - buy_vwap) / r * 10_000.0
    return EdgeResult(OK, gross, fee, gross - fee, buy_vwap, sell_vwap, r, "buy_mid", size, bd, sd)


def maker_taker_expected_edge_bps(q: float, side: str, hedge_book_now, size: float, maker_fee_bps: float, taker_fee_bps: float, *,
                                  expected_hedge_move_bps: float = 0.0, expected_hedge_slippage_bps: float = 0.0,
                                  funding_holding_bps: float = 0.0, p_fail: float = 0.0, loss_fail_bps: float = 0.0,
                                  ref: float | None = None) -> EdgeResult:
    """Conditional expected edge of resting at price q on the maker venue (side "bid": we buy at q and the hedge SELLS on
    hedge_book_now's bid; side "ask": we sell at q and the hedge BUYS on its ask).

        edge = (E[hedge price | fill] - q) / ref        (bid)   or   (q - E[hedge price | fill]) / ref   (ask)
               - maker_fee - taker_fee - E[hedge slippage] - funding/holding - p_fail x loss_fail

    E[hedge price | fill] = hedge touch now x (1 + expected_hedge_move_bps / 1e4) for a sell hedge (a negative move lowers the bid:
    adverse) and x (1 - expected_hedge_move_bps / 1e4) for a buy hedge (a negative move raises the ask: adverse). ref defaults to
    the price we pay: q for a bid, the expected hedge price for an ask (ref_name "buy_price")."""
    fee = maker_fee_bps + taker_fee_bps
    if hedge_book_now is None or not hedge_book_now.ready or q <= 0 or size <= 0:
        return EdgeResult(NO_BOOK, None, fee, None, None, None, None, "buy_price", size)
    touch = hedge_book_now.best_bid if side == "bid" else hedge_book_now.best_ask
    if touch <= 0:
        return EdgeResult(NO_BOOK, None, fee, None, None, None, None, "buy_price", size)
    if side == "bid":
        expected = touch * (1.0 + expected_hedge_move_bps / 10_000.0)
        r = ref if ref is not None else q
        gross = (expected - q) / r * 10_000.0
        naive = (touch - q) / r * 10_000.0
        buy_vwap, sell_vwap = q, expected
    else:
        expected = touch * (1.0 - expected_hedge_move_bps / 10_000.0)
        r = ref if ref is not None else expected
        gross = (q - expected) / r * 10_000.0
        naive = (q - touch) / r * 10_000.0
        buy_vwap, sell_vwap = expected, q
    deductions = {"maker_fee_bps": maker_fee_bps, "taker_fee_bps": taker_fee_bps, "hedge_slippage_bps": expected_hedge_slippage_bps,
                  "funding_holding_bps": funding_holding_bps, "failure_cost_bps": p_fail * loss_fail_bps, "hedge_move_bps": expected_hedge_move_bps}
    net = gross - fee - expected_hedge_slippage_bps - funding_holding_bps - p_fail * loss_fail_bps
    return EdgeResult(OK, gross, fee, net, buy_vwap, sell_vwap, r, "buy_price", size, naive_bps=naive, components=deductions)


# ── legacy_v0: the expressions used before 2026-11-01, unchanged ───────────────────────────────────────────────────────

def legacy_taker_taker_edge_bps(buy_px: float, sell_px: float, buy_fee_bps: float, sell_fee_bps: float) -> tuple[float, float]:
    """(gross, net) with gross = (sell_bid - buy_ask) / buy_ask x 1e4 (top of book, reference = buy ask); net = gross - fees."""
    gross = (sell_px - buy_px) / buy_px * 10_000.0
    return gross, gross - (buy_fee_bps + sell_fee_bps)


def legacy_maker_bid_edge_bps(hedge_px: float, max_bid: float, fee_bps: float) -> tuple[float, float]:
    """(gross, net) with gross = (hedge_bid - max_bid) / max_bid x 1e4 (reference = our maker price)."""
    gross = (hedge_px - max_bid) / max_bid * 10_000.0
    return gross, gross - fee_bps


def legacy_maker_ask_edge_bps(min_ask: float, hedge_px: float, fee_bps: float) -> tuple[float, float]:
    """(gross, net) with gross = (min_ask - hedge_ask) / hedge_ask x 1e4 (reference = the hedge price we pay)."""
    gross = (min_ask - hedge_px) / hedge_px * 10_000.0
    return gross, gross - fee_bps
