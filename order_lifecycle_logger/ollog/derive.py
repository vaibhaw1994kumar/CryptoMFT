"""Latency figures derived from one order record's raw timestamps.

Two families, kept apart because their error differs:
  * local  - differences of `time.monotonic_ns` stamps taken on this machine only. Immune to clock offset.
  * exchange_referenced - the exchange's own timestamps against this machine's wall clock, shifted by the latest measured
    server-minus-local offset. Each carries +/- `clock.uncertainty_ms` (half the round trip of the best time probe), and the
    exchange stamps are whole milliseconds. Treat differences below that as noise.
Anything whose inputs are missing is None; nothing is filled in.
"""
from __future__ import annotations


def _ms(a, b):
    return None if a is None or b is None else (b - a) / 1e6


def derive(rec: dict) -> dict:
    t, ex, clock = rec["t"], rec["ex"], rec.get("clock") or {}
    local = {
        "place_rest_rtt": _ms(t.get("send_mono_ns"), t.get("rest_mono_ns")),
        "place_send_to_ws_new": _ms(t.get("send_mono_ns"), t.get("wsnew_mono_ns")),
        "place_send_to_ws_reject": _ms(t.get("send_mono_ns"), t.get("wsrej_mono_ns")),
        "ws_new_after_rest_return": _ms(t.get("rest_mono_ns"), t.get("wsnew_mono_ns")),
        "cancel_rest_rtt": _ms(t.get("csend_mono_ns"), t.get("crest_mono_ns")),
        "cancel_send_to_ws_cancelled": _ms(t.get("csend_mono_ns"), t.get("wscan_mono_ns")),
        "ws_cancelled_after_cancel_rest_return": _ms(t.get("crest_mono_ns"), t.get("wscan_mono_ns")),
        "place_send_to_cancel_confirmed": _ms(t.get("send_mono_ns"), t.get("wscan_mono_ns")),
    }
    off = clock.get("offset_ms")

    def wall_ms(key):
        v = t.get(key)
        return None if v is None or off is None else v / 1e6 + off

    def gap(a, b):
        return None if a is None or b is None else b - a

    exch = {
        "place_send_to_exchange_rest_time": gap(wall_ms("send_wall_ns"), ex.get("rest_ms")),
        "place_send_to_exchange_new_event": gap(wall_ms("send_wall_ns"), ex.get("wsnew_event_ms")),
        "exchange_new_event_to_ws_recv": gap(ex.get("wsnew_event_ms"), wall_ms("wsnew_wall_ns")),
        "cancel_send_to_exchange_rest_time": gap(wall_ms("csend_wall_ns"), ex.get("crest_ms")),
        "cancel_send_to_exchange_cancel_event": gap(wall_ms("csend_wall_ns"), ex.get("wscan_event_ms")),
        "exchange_cancel_event_to_ws_recv": gap(ex.get("wscan_event_ms"), wall_ms("wscan_wall_ns")),
    }
    return {"local_ms": local, "exchange_referenced_ms": exch, "exchange_referenced_uncertainty_ms": clock.get("uncertainty_ms")}
