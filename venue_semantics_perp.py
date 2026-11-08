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

Retry (Phase C, 8 Nov 2026): every REJECT row says whether the SAME request may be sent again (Mapped.retry_permitted). Only a transient refusal that created no order
(a rate limit, an IOC that found nothing inside its cap, a cap breached before dispatch: the last two are engine rows, not venue codes) is retryable. A margin, minimum-size, reduce-only or position-mode refusal, a PostOnly refusal and any generic reject are not: the same request would be refused
again, or the cause is unknown, and a blind resend of a hedge is not allowed. Not retryable on a hedge means the hedge goes to the unwind path, not that it is dropped.
`classify_reject` is the one entry point the OMS uses for every reject; a reason it cannot place is UNCLASSIFIED and not retryable (fail closed), and the unclassified count is reported.
Moved from new_backtest/venue_semantics_perp.py (a re-export stays there until Phase F); the table rows are unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple

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

    @property
    def retry_permitted(self) -> bool | None:
        """For a REJECT row: may the same request be sent again. None for every other kind (not a reject)."""
        return (self.label in RETRYABLE_LABELS) if self.kind is Kind.REJECT else None


RETRYABLE_LABELS = frozenset({"rate_limit", "ioc_unfilled", "price_cap_exceeded"})      # transient, no order created; everything else that rejects is not retried blind


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

# ---- engine-side refusals (no venue code, so not rows of the venue table above: mapping_table() stays observed / spec only) --------------------
# nothing was executed; the same request may be tried again when the market allows
_ENGINE_REJECTS: dict[str, tuple[str, str]] = {
    "ioc_expired_unfilled": ("ioc_unfilled", "an IOC hedge found nothing to execute inside its price cap"),
    "price_cap_exceeded": ("price_cap_exceeded", "the hedge book moved past the order's worst price before dispatch"),
    "expired_at_release": ("expired_at_release", "a queued request was found, at release, to be no longer worth sending (EMS01); not retried blind"),
}

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


class RejectClass(NamedTuple):
    label: str                  # table label, or "unclassified"
    retry_permitted: bool       # False when unclassified (fail closed)
    basis: str                  # observed | spec | none
    classified: bool


_UNCLASSIFIED = RejectClass("unclassified", False, "none", False)


def classify_reject(market: str | None, reason: str) -> RejectClass:
    """The one reject classifier. `reason` is a REST code, a stream rejectReason or a text carrying one of them; `market` is BYBIT / BINANCE or None when the adapter does not say.
    Looks for the code as a REST row, then as a stream reason on any status of that market. Anything else is UNCLASSIFIED and not retryable (a simulated adapter's free text lands here)."""
    reason = str(reason or "").strip()
    if not reason:
        return _UNCLASSIFIED
    head = reason.split(":", 1)[0].strip()
    if head in _ENGINE_REJECTS:
        return RejectClass(_ENGINE_REJECTS[head][0], _ENGINE_REJECTS[head][0] in RETRYABLE_LABELS, "engine", True)
    markets = (market,) if market else (BYBIT, BINANCE)
    for m in markets:
        row = _T.get((m, "REST", reason))
        if row is None:
            for (rm, _status, rr), v in _T.items():
                if rm == m and rr == reason and v.kind is Kind.REJECT:
                    row = v
                    break
        if row is not None and row.kind is Kind.REJECT:
            return RejectClass(row.label, bool(row.retry_permitted), row.basis, True)
    return _UNCLASSIFIED
