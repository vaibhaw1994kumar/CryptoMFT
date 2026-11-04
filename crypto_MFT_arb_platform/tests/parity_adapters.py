"""Adapters that feed one event script (new_backtest.order_state_machine.Event) into each implementation of the order lifecycle.

    spec   order_state_machine.replay                                  the written-down rules
    oms    crypto_stat_arb.oms.OMS.ingest_order_update                 the core OMS reducer, silent venue adapters
    maker  MakerExecutor + CrossVenueArbBacktester lifetime code        the backtester's passive-order lifecycle

Each returns an Outcome(state, booked, fills) in the spec's vocabulary, or raises NotRepresentable(reason) when the
implementation has no way to express the event. Nothing here changes either implementation.
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass
from decimal import Decimal

from new_backtest.order_state_machine import CUM_KINDS, Event, Kind, State, replay

SIZE = 2.0          # maker order size, base units
EPS = 1e-9


class NotRepresentable(Exception):
    pass


@dataclass(frozen=True)
class Outcome:
    state: str
    booked: float
    fills: tuple
    booked_at: tuple = ()          # time at which the strategy's books took each increment (same order as `fills`)

    @staticmethod
    def of(state, booked, fills, booked_at=()):
        return Outcome(str(state), round(float(booked), 6), tuple(round(float(f), 6) for f in fills),
                       tuple(round(float(t), 4) for t in booked_at))


def run_spec(events) -> Outcome:
    r = replay(events)
    booked, at = 0.0, []
    for e in events:
        if e.kind in CUM_KINDS and e.cum > booked + EPS:
            at.append(e.arrive)
            booked = e.cum
    return Outcome.of(r.state, r.booked, r.fills, at)


# ── core OMS ─────────────────────────────────────────────────────────────────

def run_oms(events) -> Outcome:
    from crypto_stat_arb.execution import CanonicalOrderUpdate, ExecutionEventType, OrderUpdateSource, StrategyDirectedRouter
    from crypto_stat_arb.ledger import Ledger
    from crypto_stat_arb.models import OrderState
    from crypto_stat_arb.oms import OMS
    from test_oms import instrument_master, make_book, opportunity, risk

    class SilentAdapter:
        """Accepts every request and says nothing: the order stays SENT until an update is ingested."""
        def handle(self, request, book):
            return ()

    instrument = "BTC-USDT-SPOT"
    books = {("BINANCE", instrument): make_book("BINANCE", "59999.90", "60000.00"),
             ("BYBIT", instrument): make_book("BYBIT", "60060.00", "60060.10")}
    ledger = Ledger()
    oms = OMS(instrument_master(), risk(), ledger,
              router=StrategyDirectedRouter(instrument_master(), adapters={"BINANCE": SilentAdapter(), "BYBIT": SilentAdapter()}))
    plan = oms.execute(opportunity(), books)
    order = plan.children[0]
    qty = order.quantity
    kind_to_event = {Kind.ACK: ExecutionEventType.ACKNOWLEDGED, Kind.REJECT: ExecutionEventType.REJECTED,
                     Kind.CANCEL_CONFIRM: ExecutionEventType.CANCELLED, Kind.TIMEOUT: ExecutionEventType.TIMED_OUT}
    n = 0
    for e in events:
        n += 1
        if e.kind == Kind.SUBMIT:
            continue
        if e.kind == Kind.CANCEL_REQUEST:
            oms.note_cancel_requested(order.order_id, int(e.ts * 1e9))     # D-05: OMS02 CANCEL_PENDING
            continue
        if e.kind == Kind.RECONCILE:
            raise NotRepresentable("reconcile is a venue query on the OMS side, not an update")
        if e.kind == Kind.FILL:
            state = ExecutionEventType.FILLED if e.cum >= 1.0 - EPS else ExecutionEventType.PARTIAL
        else:
            state = kind_to_event[e.kind]
        cum = Decimal(str(round(e.cum, 12))) * qty if e.kind in (Kind.FILL, Kind.CANCEL_CONFIRM) else Decimal("0")
        source = OrderUpdateSource.TIMER if e.kind == Kind.TIMEOUT else OrderUpdateSource.PRIVATE_STREAM
        oms.ingest_order_update(CanonicalOrderUpdate(
            update_id=f"u{n}", source=source, parent_order_id=plan.parent_id, order_id=order.order_id, venue=order.venue,
            instrument_id=order.instrument_id, state=state, event_ts_ns=int(e.ts * 1e9), observed_ts_ns=int(e.arrive * 1e9) + 1,
            sequence=n, cumulative_filled_quantity=cum, average_price=Decimal("60000")))
    # The OMS02 correspondence (crypto_stat_arb.oms, above ALLOWED_ORDER_TRANSITIONS): SENT = SUBMITTED,
    # CANCEL_PENDING = CANCEL_REQUESTED, TIMED_OUT = UNKNOWN. CREATED is never seen here (the order is sent by execute()).
    mapping = {OrderState.CREATED: State.SUBMITTED, OrderState.SENT: State.SUBMITTED, OrderState.ACKNOWLEDGED: State.RESTING,
               OrderState.PARTIAL: State.PARTIALLY_FILLED, OrderState.FILLED: State.FILLED, OrderState.REJECTED: State.REJECTED,
               OrderState.CANCELLED: State.CANCELLED, OrderState.TIMED_OUT: State.UNKNOWN,
               OrderState.CANCEL_PENDING: State.CANCEL_REQUESTED}
    rows = [x for x in ledger.events if x["event_type"] == "FILL"]
    return Outcome.of(mapping[order.state], order.filled_quantity / qty, [Decimal(str(x["fill_quantity"])) / qty for x in rows],
                      [x["ts_ns"] / 1e9 for x in rows])


# ── backtester maker lifecycle ───────────────────────────────────────────────

def run_maker(events, *, cfg_kw=None, unknown_model=False) -> Outcome:
    """Drive one resting buy through MakerExecutor and the backtester's own expiry/cancel code (Harness of
    tests/test_maker_order_lifetime.py). Event times are used as replay times. A cancel request is expressed the only way the
    backtester knows it: a lifetime (ArbConfig.maker_max_age_s) that expires at the request time, followed by one cancel latency.

    Explicit states (task 6): with postonly_reject_model="on" a REJECT before any fill is a PostOnly refusal at arrival (D-01) and a REJECT after a
    fill is a venue reject notice (bt.venue_reject_notice: the fill is kept, one alarm, PARTIALLY_FILLED). With unknown_model=True a TIMEOUT is an
    ack that never came (HedgeFaultConfig.ack_unknown_prob=1, ack_timeout_s, unknown_resolve_s from the script): the order is UNKNOWN until the first
    later piece of evidence arrives (D-02). The end state is read from MakerExecutor.lifecycle_state, not inferred from counters."""
    from new_backtest.event_loader import TRADE
    from new_backtest.hedge_faults import HedgeFaultConfig
    from test_maker_order_lifetime import BINANCE, BYBIT, Harness

    kinds = {e.kind for e in events}
    cfg_kw = dict(cfg_kw or {})
    po_on = cfg_kw.get("postonly_reject_model") == "on"
    ordered = sorted(events, key=lambda x: x.arrive)
    first_fill = next((e.arrive for e in ordered if e.kind == Kind.FILL), None)
    rej = next((e for e in ordered if e.kind == Kind.REJECT), None)
    late_reject = rej is not None and first_fill is not None and rej.arrive > first_fill
    for k, why in ((Kind.REJECT, "the simulator has no exchange rejection of a passive order (postonly_reject_model is off)"),
                   (Kind.TIMEOUT, "the simulator has no acknowledgement, so no acknowledgement timeout and no unknown state (unknown_model off)"),
                   (Kind.RECONCILE, "the simulator has no venue query")):
        if k in kinds and not (k == Kind.REJECT and po_on) and not (k == Kind.TIMEOUT and unknown_model):
            raise NotRepresentable(why)
    submit = next(e for e in events if e.kind == Kind.SUBMIT)
    ack = next((e for e in events if e.kind == Kind.ACK), None)
    place_t = submit.arrive + 0.003          # the order is live from its arrival; an ack REPORT has no separate time in the simulator
    creq = next((e for e in events if e.kind == Kind.CANCEL_REQUEST), None)
    cconf = next((e for e in events if e.kind == Kind.CANCEL_CONFIRM), None)
    ttl = (creq.ts - place_t) if creq is not None else 0.0
    cancel_s = (cconf.arrive - creq.ts) if (creq is not None and cconf is not None) else 1e6
    faults = None
    to = next((e for e in ordered if e.kind == Kind.TIMEOUT), None)
    if to is not None:
        later = [e.arrive for e in ordered if e.arrive > to.arrive and e.kind in (Kind.ACK, Kind.FILL, Kind.REJECT, Kind.CANCEL_CONFIRM, Kind.RECONCILE)]
        faults = HedgeFaultConfig(ack_unknown_prob=1.0, ack_timeout_s=to.ts - place_t, unknown_resolve_s=(min(later) - to.ts) if later else 1e6)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        h = Harness(ttl=ttl if ttl > 0 else 0.0, entry_s=max(0.001, place_t - submit.arrive), cancel_s=cancel_s, queue=0.0, max_live=4, cfg_kw=cfg_kw, faults=faults)
        h.bt.latency.aligner.samples = 1                   # exchange times usable (offset 0)
        h.book(-1.0, BYBIT)
        h.book(-1.0, BINANCE)
        # A reject before any fill is expressed the one way this simulator knows: a PostOnly order priced at the best ask (101.0) when it arrives.
        h.send_maker(submit.arrive, qty=SIZE, price=101.0 if (Kind.REJECT in kinds and not late_reject) else 100.0)
        if po_on:
            # with the PostOnly model on, an arrival is accepted or refused when the feed delivers the first event stamped at/after it: a benign
            # buy-side print far above the book (it can fill nothing) stands for that event
            h.event(place_t, TRADE, BYBIT, {"price": 150.0, "volume": 0.001, "side": "b"}, exchange_ts=place_t)
        have = 0.0
        for e in sorted(events, key=lambda x: x.arrive):
            if e.kind in (Kind.SUBMIT, Kind.ACK):
                h.advance(e.arrive)
            elif e.kind == Kind.REJECT and late_reject:
                h.advance(e.arrive)
                mo = next(iter(h.bt.maker.orders.values()), None)
                if mo is not None:
                    h.bt.venue_reject_notice(mo.order_id, e.arrive, "venue_reject", source="injected")
            elif e.kind == Kind.FILL:
                h.advance(e.arrive)
                want = e.cum * SIZE - have
                if want > EPS and (h.resting or (po_on and not h.bt.maker.lifecycle_counts.get("rejected"))):
                    # with the PostOnly model on the order rests only once the feed delivers its first event, which is this trade
                    h.sell_trade(e.arrive, BYBIT, volume=want, price=100.0, exchange_ts=e.ts)
                    have = sum(r["qty"] for r in h.bt.trade_log if r["venue"] == BYBIT and not r["taker"])
                else:
                    h.advance(e.arrive)
            else:
                h.advance(e.arrive)
        last = max(e.arrive for e in events) + 1.0
        if po_on or cfg_kw.get("cancel_time_basis") == "exchange":
            h.event(last, TRADE, BYBIT, {"price": 150.0, "volume": 0.001, "side": "b"}, exchange_ts=last)   # a venue event: the check runs before it
        h.advance(last + 1.0)
        bt = h.bt
        rows = [r for r in bt.trade_log if r["venue"] == BYBIT and not r["taker"]]
        fills = [r["qty"] / SIZE for r in rows]
        booked = sum(fills)
        booked_at = [r["ts"] for r in rows]
        inflight = next(iter(bt.maker.lifecycle), None)
        explicit = bt.maker.lifecycle_state(inflight, last + 1.0) if inflight is not None else None
        if explicit is not None:
            state = State(explicit)                 # the executor's own explicit state (task 6), in the shared vocabulary
        else:
            if h.resting:
                o = h.resting[0]
                state = State.CANCEL_REQUESTED if o.cancel_requested_ts is not None else (State.PARTIALLY_FILLED if o.filled_qty > EPS else State.RESTING)
            elif booked >= 1.0 - EPS:
                state = State.FILLED
            elif bt.lifetime["cancels_applied"] > 0:
                state = State.CANCELLED
            elif bt.po["rejects"] > 0:
                state = State.REJECTED
            elif any(True for _ in bt.pending.in_flight()):
                state = State.SUBMITTED
            else:
                state = State.SUBMITTED

        return Outcome.of(state, booked, fills, booked_at)
