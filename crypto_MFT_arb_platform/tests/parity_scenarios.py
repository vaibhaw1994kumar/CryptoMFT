"""Event scripts shared by the lifecycle parity tests. Times are seconds; `arrive` is when the strategy learns of the event."""
from __future__ import annotations

from new_backtest.order_state_machine import Event, Kind

E = Event
K = Kind


def ack(t=0.01): return E(K.ACK, ts=t, arrive=t)
def submit(t=0.0): return E(K.SUBMIT, ts=t, arrive=t)
def fill(cum, ts, arrive=None): return E(K.FILL, ts=ts, arrive=ts if arrive is None else arrive, cum=cum)
def creq(t): return E(K.CANCEL_REQUEST, ts=t, arrive=t)
def cconf(ts, arrive=None, cum=0.0): return E(K.CANCEL_CONFIRM, ts=ts, arrive=ts if arrive is None else arrive, cum=cum)


SCENARIOS: dict[str, list[Event]] = {
    "normal_fill": [submit(), ack(), fill(1.0, 0.5)],
    "partial_then_full": [submit(), ack(), fill(0.4, 0.5), fill(1.0, 0.9)],
    "reject": [submit(), E(K.REJECT, ts=0.01, arrive=0.01)],
    "cancel_ok": [submit(), ack(), creq(1.0), cconf(1.05)],
    "fill_inside_cancel_window": [submit(), ack(), creq(1.0), fill(0.4, 1.02), cconf(1.05, cum=0.4)],
    "partial_then_cancel": [submit(), ack(), fill(0.4, 0.5), creq(1.0), cconf(1.05, cum=0.4)],
    "cancel_loses_race_to_full_fill": [submit(), ack(), creq(1.0), fill(1.0, 1.02)],
    # The fill executed at 1.02, before the cancel took effect at 1.05, but its report reaches the strategy at 1.20, after
    # the cancel confirmation (which, sent before the fill was known to the strategy, reports cum 0).
    "late_fill_after_cancel": [submit(), ack(), creq(1.0), cconf(1.05, cum=0.0), fill(0.4, 1.02, arrive=1.20)],
    "ack_timeout_then_fill": [submit(), E(K.TIMEOUT, ts=2.0, arrive=2.0), fill(1.0, 0.5, arrive=2.5)],
    "ack_timeout_then_ack": [submit(), E(K.TIMEOUT, ts=2.0, arrive=2.0), E(K.ACK, ts=0.5, arrive=3.0)],
    "ack_timeout_unresolved": [submit(), E(K.TIMEOUT, ts=2.0, arrive=2.0)],
    # Nothing is reported between 1.0 and 5.0 (the connection is down); both fills executed in the gap and arrive at reconnect.
    "disconnect_then_fills_in_gap": [submit(), ack(), fill(0.3, 1.5, arrive=5.0), fill(1.0, 2.5, arrive=5.0)],
    "out_of_order_updates": [submit(), fill(1.0, 0.5, arrive=0.6), E(K.ACK, ts=0.01, arrive=0.7), fill(0.4, 0.4, arrive=0.8)],
    # A cancel is sent and nothing comes back yet: the order is still live and can fill.
    "cancel_pending_at_end": [submit(), ack(), creq(1.0)],
    # A reject report after a fill report is a protocol violation; the fill is a fact and must not be lost.
    "reject_after_partial_fill": [submit(), ack(), fill(0.4, 0.5), E(K.REJECT, ts=1.0, arrive=1.0)],
    "duplicate_fill_report": [submit(), ack(), fill(0.4, 0.5), fill(0.4, 0.5, arrive=0.6)],
}
