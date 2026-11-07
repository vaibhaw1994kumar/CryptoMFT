"""S0 (owner 7 Nov): the simulator side of quote repricing. A thin, replaceable glue between the backtester and the core rule: NO rule lives here.
  rule        crypto_stat_arb/ems/tactics/passive.py  decide_reprice / RepriceConfig  (what to do, from numbers the trader supplies)
  target      new_backtest/arb_strategy.py  ArbStrategy.requote_target               (the edge recomputed at the CURRENT hedge-venue price, one place)
  mechanics   here: watch the hedge venue, ask the rule, send the amend after a sampled entry latency, apply it at the exchange with the PostOnly check on the
              arrival-time book, lose the queue place (MakerExecutor.amend), keep the old order resting and fillable until then (catalogue H30), count everything.
Off (execution.primary.reprice = "none", the default) this class is never built and nothing changes. The numbers are not set here: they come from the profile
(`execution.primary.reprice`, labelled TRADER_SET / LIVE_VALUES / ILLUSTRATIVE_NOT_FITTED)."""
from __future__ import annotations

from collections import Counter

from crypto_stat_arb.ems.tactics.passive import (AMEND, CANCEL, CANCEL_REPLACE, HOLD, RepriceConfig, RepriceState, after_amend_rejected, decide_reprice)

LOG_LIMIT = 20_000


class Requoter:
    def __init__(self, bt, cfg: RepriceConfig) -> None:
        self.bt, self.cfg = bt, cfg
        self.states: dict[int, RepriceState] = {}           # current maker order id -> the rule's memory of its quote (the quote keeps its start time across amends)
        self.pending: dict[int, dict] = {}                  # maker order id -> {target, effective_ts, kind, reason, filled_at_request}
        self.stats: Counter = Counter()
        self.holds: Counter = Counter()
        self.log: list[dict] = []
        self.amends_per_quote: Counter = Counter()          # quote start ts -> amends applied

    # ── called by the backtester ────────────────────────────────────────────────────────────────────────────────────────────────
    def on_hedge_event(self, venue: str, now: float) -> None:
        """A book event of `venue` was applied: `venue` is the HEDGE venue of every resting quote on the other venue. Ask the rule about each of them."""
        bt = self.bt
        for order in list(bt.maker.orders.values()):
            if order.venue == venue or not order.is_live or order.cancel_requested_ts is not None or now + 1e-12 < order.active_ts:
                continue
            st = self.states.get(order.order_id)
            if st is None:
                st = self.states[order.order_id] = RepriceState(quote_start_ts=order.placed_ts)
            buy = order.side_str == "buy"
            hedge_book = bt.books[venue]
            hedge_px = hedge_book.best_bid if buy else hedge_book.best_ask
            if hedge_px > 0:
                st.observe_hedge(self.cfg, now, hedge_px)
            target, why = bt.strategy.requote_target(order.venue, order.side_str, order.remaining, bt.books, now)
            if why in ("stale_hedge_touch", "no_hedge_price"):
                self.holds[why] += 1                          # never act on a touch we cannot trust
                continue
            d = decide_reprice(self.cfg, order.side_str, order.price, target, now, st, order.queue_ahead)
            if d.action == HOLD:
                self.holds[d.reason] += 1
                continue
            if d.action == CANCEL:
                self._cancel(order, st, now, d.reason)
            else:
                self._request(order, st, now, d, target)

    def apply_due(self, venue: str, ets: float | None, now: float) -> None:
        """Amends whose effective time has been reached on `venue` take effect before the first venue event stamped at or after it (the cancel rule)."""
        bt = self.bt
        t_cmp = ets if ets is not None else now
        for oid, p in list(self.pending.items()):
            order = bt.maker.orders.get(oid)
            v = order.venue if order is not None else p["venue"]
            if v != venue:
                continue
            if not (t_cmp > p["effective_ts"] or (t_cmp == p["effective_ts"] and not bt._market_first)):
                continue
            self._apply(oid, p, order, now)

    # ── mechanics ───────────────────────────────────────────────────────────────────────────────────────────────────────────────
    def _delay(self, venue: str, kind: str) -> float:
        bt = self.bt
        draw = (lambda name: bt.timelines.sample_s(venue, name)) if bt.timelines is not None else None
        entry = draw("entry") if draw else bt.latency.entry_delay(venue)
        if kind == AMEND:
            return entry                                       # one request: it reaches the venue like an order does
        return (draw("cancel") if draw else bt.latency.cancel_delay(venue)) + entry   # cancel, then a new order

    def _request(self, order, st: RepriceState, now: float, d, target: float) -> None:
        eff = now + self._delay(order.venue, d.action)
        self.pending[order.order_id] = {"target": target, "effective_ts": eff, "kind": d.action, "reason": d.reason, "venue": order.venue, "requested_ts": now,
                                        "filled_at_request": order.filled_qty, "from_price": order.price}
        st.pending, st.last_request_ts = True, now
        self.stats["requests_" + d.action.lower()] += 1
        self.stats["reason_" + d.reason] += 1
        self._note(now, order, d.action, d.reason, order.price, target, eff)

    def _cancel(self, order, st: RepriceState, now: float, reason: str) -> None:
        self.bt._request_cancel(order, now, "requote_" + reason)
        st.pending, st.last_request_ts = True, now
        self.stats["requests_cancel"] += 1
        self.stats["reason_" + reason] += 1
        self._note(now, order, CANCEL, reason, order.price, None, order.cancel_effective_ts)

    def _apply(self, oid: int, p: dict, order, now: float) -> None:
        bt, cfg = self.bt, self.cfg
        self.pending.pop(oid, None)
        st = self.states.get(oid)
        if order is None or not order.is_live:
            self.stats["amend_order_gone"] += 1               # filled or cancelled while the request was in flight: a fill is a fill, nothing is resent (OMS03)
            self.states.pop(oid, None)
            return
        if order.cancel_requested_ts is not None:
            self.stats["amend_skipped_cancel_pending"] += 1
            if st is not None:
                st.pending = False
            return
        fills_in_window = order.filled_qty - p["filled_at_request"]
        if fills_in_window > 1e-12:
            self.stats["fills_inside_amend_window"] += 1
            self.stats["qty_filled_inside_amend_window"] += fills_in_window      # the old order stayed fillable until the amend landed: kept and hedged as any fill (H30)
        eff = p["effective_ts"]
        venue = order.venue
        view = bt.books[venue]
        book = self._arrival_book(venue, eff, view)
        sid = bt._maker_to_signal.get(oid, 0)
        intent = bt._maker_to_intent.get(oid, "")
        state_v = bt.states[venue]
        res = bt.maker.amend(oid, p["target"], order.remaining, book=book, state=state_v.to_market_state(view, now), regime_bucket=state_v.regime().bucket, now=eff,
                             active_ts=eff, tick_size=bt.tick_size[venue], postonly=bt.config.postonly_reject_model == "on", arrival_ts=eff)
        if res.order is None:
            if res.reason == "unknown_order":
                self.stats["amend_order_gone"] += 1
                self.states.pop(oid, None)
                return
            # the venue refused the replacement (PostOnly would take liquidity): the engine has already removed the old order, and a fresh placement at that price
            # would be refused as well, so nothing rests (after_amend_rejected: no target that rests).
            self.stats["amend_rejected_postonly"] += 1
            after_amend_rejected(cfg, None, order_gone=False)
            self._note(eff, order, "REJECTED", res.reason, p["from_price"], p["target"], eff)
            self.states.pop(oid, None)
            tracker = bt._trackers.get(sid)
            bt._cancel_acks.append(eff + (bt.timelines.sample_s(venue, "cancel_report") if bt.timelines is not None else bt.venue_specs[venue].latency.feed_ms / 1000.0))
            if tracker is not None:
                tracker.maker_resting = False
            bt._maybe_resolve(sid, now)
            return
        new = res.order
        bt._maker_to_signal[new.order_id] = sid
        bt._maker_to_intent[new.order_id] = intent
        bt._maker_to_signal.pop(oid, None)
        bt._maker_to_intent.pop(oid, None)
        bt.maker.lc_amended(oid, new, eff)
        if st is not None:
            st.pending, st.reprices = False, st.reprices + 1
            st.quote_start_ts = eff                          # the quote's age counts from its last (re)placement: a max-age re-quote starts a new quote, it does not repeat every interval
            self.states[new.order_id] = st
        self.states.pop(oid, None)
        self.amends_per_quote[st.quote_start_ts if st is not None else new.placed_ts] += 1
        self.stats["amends_applied"] += 1
        self._note(eff, new, "AMENDED", p["reason"], p["from_price"], new.price, eff)
        if bt._obs_on:
            bt._obs_requote_place({"ts": eff, "maker_order_id": new.order_id, "amended_from": oid, "signal_id": sid, "intent_id": intent, "venue": venue, "side": new.side_str,
                                       "price": new.price, "size": new.size, "queue_ahead": new.queue_ahead, "level_size": new.observed_level_size,
                                       "level_was_absent": new.level_was_absent, "quote_start_ts": st.quote_start_ts if st is not None else None, "reason": p["reason"]})

    def _arrival_book(self, venue: str, eff: float, view):
        """The book the amend is checked against: the venue-truth book at the arrival's exchange time when it is complete there, else the strategy view (counted)."""
        bt = self.bt
        if not bt._truth_on:
            return view
        aligner = bt.latency.aligner
        x = aligner.to_exchange(eff) if aligner.ready else eff
        bt._truth_catch_up(venue, x, eff)
        q = bt._ahead[venue]
        tb = bt.truth_books[venue]
        if tb.ready and q and (getattr(q[0], "exchange_ts", None) or 0.0) > x:
            return tb
        self.stats["amend_view_book_fallback"] += 1
        return view

    def _note(self, ts: float, order, kind: str, reason: str, from_price, to_price, effective_ts) -> None:
        if len(self.log) < LOG_LIMIT:
            self.log.append({"ts": ts, "maker_order_id": order.order_id, "signal_id": self.bt._maker_to_signal.get(order.order_id, 0), "kind": kind, "reason": reason,
                             "from_price": from_price, "to_price": to_price, "effective_ts": effective_ts})

    def report(self) -> dict:
        per = list(self.amends_per_quote.values())
        return {"config": self.cfg.as_dict(), "stats": dict(self.stats), "holds": dict(self.holds), "quotes_amended": len(per), "amends_per_amended_quote_max": max(per) if per else 0,
                "amends_total": sum(per), "pending_unapplied_at_end": len(self.pending), "events": self.log}
