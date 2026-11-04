"""Perpetual venue message semantics: native status / reason code -> the shared order-lifecycle events (order_state_machine.Kind).

Bybit linear (V5 private order stream, orderStatus + rejectReason) and Binance USD-M (ORDER_TRADE_UPDATE x/X, REST error code).
Shared vocabulary: order_state_machine.State / Kind (backtester names; OMS02 names: CANCEL_PENDING = CANCEL_REQUESTED, TIMED_OUT = UNKNOWN).

Every row says WHERE it comes from:
    observed   seen in the ollog TESTNET logs (counts are produced by scripts/perp_message_semantics.py from the recorded runs)
    spec       NOT seen in any log. The mapping is taken from the venue's documented behaviour and is unverified here.
               It exists so that an unmapped status raises instead of being guessed; it is not evidence about the venue.

A PostOnly (maker-only) order that would take liquidity maps to REJECT with label 'postonly_reject' on both venues, never to a normal cancel:
    Bybit linear   orderStatus Cancelled with rejectReason EC_PostOnlyWillTakeLiquidity, no 'New' before it (observed).
    Binance USD-M  GTX: REST error -5022 on the place request, no order is created, no stream event (observed on testnet);
                   a stream status EXPIRED on a GTX order that was not cancelled by us would also be 'postonly_reject' (spec, not observed).

A status or reason that is not in the table raises UnmappedVenueStatus (code ORDER_TRANSITION_UNEXPECTED); nothing falls back to 'cancelled'.
"""
from __future__ import annotations

from dataclasses import dataclass

from .order_state_machine import Kind

ERROR_CODE = "ORDER_TRANSITION_UNEXPECTED"
BYBIT = "bybit_linear"
BINANCE = "binance_futures"


class UnmappedVenueStatus(ValueError):
    code = ERROR_CODE

    def __init__(self, market: str, status: str, reason: str):
        super().__init__(f"{ERROR_CODE}: unmapped {market} status={status!r} reason/code={reason!r}")
        self.market, self.status, self.reason = market, status, reason


@dataclass(frozen=True)
class Mapped:
    kind: Kind                  # shared event kind
    label: str                  # finer meaning: ack | cancel_confirmed | postonly_reject | fill | partial_fill | ...
    basis: str                  # observed | spec
    terminal: bool
    note: str = ""


# (market, native status, reason/code) -> Mapped.  reason "*" = any reason NOT listed explicitly for that status.
_T: dict[tuple[str, str, str], Mapped] = {}


def _row(market, status, reason, kind, label, basis, terminal, note=""):
    _T[(market, status, reason)] = Mapped(kind, label, basis, terminal, note)


# ---- Bybit linear, private order stream ------------------------------------------------------------
_row(BYBIT, "New", "EC_NoError", Kind.ACK, "ack", "observed", False)
_row(BYBIT, "Cancelled", "EC_PerCancelRequest", Kind.CANCEL_CONFIRM, "cancel_confirmed", "observed", True)
_row(BYBIT, "Cancelled", "EC_PostOnlyWillTakeLiquidity", Kind.REJECT, "postonly_reject", "observed", True,
     "arrives as Cancelled with no preceding New: the order never rested. Not a user cancel.")
_row(BYBIT, "Filled", "EC_NoError", Kind.FILL, "fill", "observed", True)
_row(BYBIT, "PartiallyFilled", "EC_NoError", Kind.FILL, "partial_fill", "spec", False)
_row(BYBIT, "PartiallyFilledCanceled", "EC_PerCancelRequest", Kind.CANCEL_CONFIRM, "cancel_confirmed_after_partial", "spec", True,
     "seen in the SPOT export, not in the perp logs")
_row(BYBIT, "Cancelled", "EC_SelfMatchPrevention", Kind.CANCEL_CONFIRM, "stp_cancel", "spec", True, "self-match prevention: venue cancel, not ours")
_row(BYBIT, "Cancelled", "EC_ReduceOnlyRejected", Kind.REJECT, "reduce_only_reject", "spec", True)
_row(BYBIT, "Rejected", "*", Kind.REJECT, "reject_other", "spec", True)
# REST ret codes on the place request (no order is created)
_row(BYBIT, "REST", "110094", Kind.REJECT, "min_order_value_reject", "observed", True, "order value below the venue minimum (5 USDT on testnet); not a PostOnly event")
_row(BYBIT, "REST", "110007", Kind.REJECT, "margin_insufficient", "spec", True, "code number from the venue's documentation, unverified here")
_row(BYBIT, "REST", "110017", Kind.REJECT, "reduce_only_reject", "spec", True, "code number from the venue's documentation, unverified here")
_row(BYBIT, "REST", "110025", Kind.REJECT, "position_mode_reject", "spec", True, "code number from the venue's documentation, unverified here")
_row(BYBIT, "REST", "10006", Kind.REJECT, "rate_limit", "spec", True, "no order created, retryable; code number unverified here")

# ---- Binance USD-M, user data stream (ORDER_TRADE_UPDATE) and REST -------------------------------
_row(BINANCE, "NEW", "*", Kind.ACK, "ack", "observed", False)
_row(BINANCE, "PARTIALLY_FILLED", "*", Kind.FILL, "partial_fill", "observed", False)
_row(BINANCE, "FILLED", "*", Kind.FILL, "fill", "observed", True)
_row(BINANCE, "CANCELED", "*", Kind.CANCEL_CONFIRM, "cancel_confirmed", "observed", True)
_row(BINANCE, "EXPIRED", "gtx_not_ours", Kind.REJECT, "postonly_reject", "spec", True,
     "GTX order expired with no fill and no cancel requested by us. Not observed: on testnet the same event was a REST -5022")
_row(BINANCE, "EXPIRED", "*", Kind.CANCEL_CONFIRM, "expired", "spec", True, "time-in-force expiry that is not a GTX refusal")
_row(BINANCE, "EXPIRED_IN_MATCH", "*", Kind.CANCEL_CONFIRM, "stp_cancel", "spec", True, "self-trade prevention expiry: venue cancel, not ours")
_row(BINANCE, "REJECTED", "*", Kind.REJECT, "reject_other", "spec", True)
_row(BINANCE, "REST", "-5022", Kind.REJECT, "postonly_reject", "observed", True, "GTX would take liquidity: refused on REST, no order created, no stream event")
_row(BINANCE, "REST", "-2019", Kind.REJECT, "margin_insufficient", "spec", True, "code number from the venue's documentation, unverified here")
_row(BINANCE, "REST", "-2022", Kind.REJECT, "reduce_only_reject", "spec", True, "code number from the venue's documentation, unverified here")
_row(BINANCE, "REST", "-4061", Kind.REJECT, "position_mode_reject", "spec", True, "position side does not match the account's position mode; unverified here")
_row(BINANCE, "REST", "-1003", Kind.REJECT, "rate_limit", "spec", True, "no order created, retryable; code number unverified here")
_row(BINANCE, "REST", "-1015", Kind.REJECT, "rate_limit", "spec", True, "order-rate limit; no order created; code number unverified here")

_STATUS_WITH_ANY_REASON = {(m, s) for (m, s, r) in _T if r == "*"}


def mapping_table() -> list[dict]:
    return [{"market": m, "native_status": s, "reason_or_code": r, "kind": v.kind.value, "label": v.label, "basis": v.basis,
             "terminal": v.terminal, "note": v.note} for (m, s, r), v in sorted(_T.items())]


def map_stream_update(market: str, status: str, reason: str = "", *, tif: str = "", cancel_requested: bool = False, cum_filled: float = 0.0) -> Mapped:
    """Private-stream order message -> Mapped. Raises UnmappedVenueStatus for anything not in the table."""
    status, reason = str(status), str(reason or "")
    if market == BINANCE and status == "EXPIRED":
        gtx_refusal = tif.upper() == "GTX" and not cancel_requested and cum_filled <= 0.0
        return _T[(BINANCE, "EXPIRED", "gtx_not_ours" if gtx_refusal else "*")]
    row = _T.get((market, status, reason))
    if row is None and (market, status) in _STATUS_WITH_ANY_REASON:
        row = _T[(market, status, "*")]
    if row is None:
        raise UnmappedVenueStatus(market, status, reason)
    return row


def map_rest_reject(market: str, code: str) -> Mapped:
    row = _T.get((market, "REST", str(code)))
    if row is None:
        raise UnmappedVenueStatus(market, "REST", str(code))
    return row
