"""Task 3 (perp lifecycle): venue statuses / reason codes -> shared lifecycle events. Message shapes are anonymised: the fields the mapping reads,
with no order, client or exec ids and no prices. Rows marked spec are not claimed to have been observed."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from new_backtest.order_state_machine import Kind
from new_backtest.venue_semantics_perp import (BINANCE, BYBIT, ERROR_CODE, UnmappedVenueStatus, map_rest_reject, map_stream_update, mapping_table)

# (market, shape of the recorded message, expected kind, expected label)
STREAM_SHAPES = [
    (BYBIT, {"orderStatus": "New", "rejectReason": "EC_NoError"}, Kind.ACK, "ack"),
    (BYBIT, {"orderStatus": "Cancelled", "rejectReason": "EC_PerCancelRequest"}, Kind.CANCEL_CONFIRM, "cancel_confirmed"),
    (BYBIT, {"orderStatus": "Cancelled", "rejectReason": "EC_PostOnlyWillTakeLiquidity"}, Kind.REJECT, "postonly_reject"),
    (BYBIT, {"orderStatus": "Filled", "rejectReason": "EC_NoError"}, Kind.FILL, "fill"),
    (BINANCE, {"X": "NEW"}, Kind.ACK, "ack"),
    (BINANCE, {"X": "PARTIALLY_FILLED"}, Kind.FILL, "partial_fill"),
    (BINANCE, {"X": "FILLED"}, Kind.FILL, "fill"),
    (BINANCE, {"X": "CANCELED"}, Kind.CANCEL_CONFIRM, "cancel_confirmed"),
]


def _args(market, shape):
    return (market, shape.get("orderStatus") or shape.get("X"), shape.get("rejectReason", ""))


@pytest.mark.parametrize("market,shape,kind,label", STREAM_SHAPES)
def test_observed_stream_shapes(market, shape, kind, label):
    m = map_stream_update(*_args(market, shape))
    assert (m.kind, m.label, m.basis) == (kind, label, "observed")


def test_postonly_would_cross_is_a_reject_not_a_cancel_on_both_venues():
    by = map_stream_update(BYBIT, "Cancelled", "EC_PostOnlyWillTakeLiquidity")
    bn_rest = map_rest_reject(BINANCE, "-5022")
    bn_exp = map_stream_update(BINANCE, "EXPIRED", tif="GTX")
    for m in (by, bn_rest, bn_exp):
        assert m.kind == Kind.REJECT and m.label == "postonly_reject"
    assert map_stream_update(BYBIT, "Cancelled", "EC_PerCancelRequest").kind == Kind.CANCEL_CONFIRM


def test_binance_expired_after_our_cancel_or_with_a_fill_is_not_a_postonly_reject():
    assert map_stream_update(BINANCE, "EXPIRED", tif="GTX", cancel_requested=True).label == "expired"
    assert map_stream_update(BINANCE, "EXPIRED", tif="GTX", cum_filled=0.5).label == "expired"
    assert map_stream_update(BINANCE, "EXPIRED", tif="GTC").label == "expired"


def test_expired_in_match_is_a_venue_cancel_labelled_stp():
    m = map_stream_update(BINANCE, "EXPIRED_IN_MATCH")
    assert (m.kind, m.label, m.basis) == (Kind.CANCEL_CONFIRM, "stp_cancel", "spec")


@pytest.mark.parametrize("market,code,label,basis", [
    (BYBIT, "110094", "min_order_value_reject", "observed"), (BINANCE, "-5022", "postonly_reject", "observed"),
    (BYBIT, "110017", "reduce_only_reject", "spec"), (BINANCE, "-2022", "reduce_only_reject", "spec"),
    (BYBIT, "110025", "position_mode_reject", "spec"), (BINANCE, "-4061", "position_mode_reject", "spec"),
    (BYBIT, "110007", "margin_insufficient", "spec"), (BINANCE, "-2019", "margin_insufficient", "spec"),
    (BYBIT, "10006", "rate_limit", "spec"), (BINANCE, "-1003", "rate_limit", "spec")])
def test_rest_reject_codes(market, code, label, basis):
    m = map_rest_reject(market, code)
    assert (m.kind, m.label, m.basis) == (Kind.REJECT, label, basis)


@pytest.mark.parametrize("call", [
    lambda: map_stream_update(BYBIT, "Cancelled", "EC_SomethingNew"),
    lambda: map_stream_update(BYBIT, "Triggered", ""),
    lambda: map_stream_update(BINANCE, "NEW_INSURANCE"),
    lambda: map_rest_reject(BINANCE, "-9999"),
    lambda: map_stream_update("kraken_perp", "NEW")])
def test_unmapped_status_raises_order_transition_unexpected(call):
    with pytest.raises(UnmappedVenueStatus) as e:
        call()
    assert e.value.code == ERROR_CODE == "ORDER_TRANSITION_UNEXPECTED"


def test_table_marks_every_row_observed_or_spec_and_never_defaults_to_cancel():
    rows = mapping_table()
    assert rows and all(r["basis"] in ("observed", "spec") for r in rows)
    assert all(r["label"] != "cancel_confirmed" for r in rows if r["reason_or_code"] == "EC_PostOnlyWillTakeLiquidity")
    spec_rows = [r for r in rows if r["basis"] == "spec"]
    assert spec_rows and all(r["note"] or r["label"] for r in spec_rows)
