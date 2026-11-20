"""Live paper / testnet forward test on real public order books.

``LiveOrderBookService`` captures and reconstructs the Binance and Bybit books;
every accepted canonical event is handed to the same ``TradingSession`` used
by the backtest (strategy, RMS, OMS/EMS, latency model). Timers for in-flight
orders run before each book update is applied, and an idle ticker runs them
between updates.

Outputs, under ``<output>/<session>/paper/``:

* ``paper_timestamps.jsonl``      exchange/receive/process timestamps for every
                                  event and decision/route timestamps for every
                                  opportunity (calibration input);
* ``canonical_events.jsonl``      the events the session saw, replayable with
                                  ``crypto-stat-arb backtest``;
* ``testnet_order_timestamps.jsonl`` send/response timestamps (testnet only);
* the same summary, TCA, ledger, latency and quality artifacts as a backtest.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import asdict, replace as _dc_replace
import json
from pathlib import Path
import time
from typing import Any, Callable

from .config import load_config
from .execution import VenueExecutionAdapter
from .fees import verify_account_fee_rates
from .margin import apply_account_tiers, read_account_tiers
from .testnet import verify_accounts_for_start
from .time_integrity import ClockMeasurement, MonotonicSessionClock, probe_server_clock, read_chrony
from .instrument_status import InstrumentStatusPoller
from .live_feed import (
    LiveFeedSettings,
    LiveOrderBookService,
    public_http_get,
    public_websocket_connector,
)
from .models import BookState, MarketEvent, json_default
from .orderbook import OrderBook
from .runner import file_sha256, write_session_artifacts
from .session import ExecutionMode, TradingSession


def perpetual_feed_settings(cfg, settings: LiveFeedSettings) -> LiveFeedSettings:
    """A perpetual config runs on linear books with the derivatives stream; spot configs are unchanged.

    Fails closed when a perpetual instrument would be fed spot order books.
    """
    from dataclasses import replace as _replace
    from .funding import is_perpetual

    perpetual = {
        venue for venue in cfg.venues
        if is_perpetual(cfg.instruments.get(venue, cfg.instrument_id).product_type)
    }
    if not perpetual:
        return settings
    markets = {"BINANCE": settings.binance_market, "BYBIT": settings.bybit_market}
    spot_fed = sorted(venue for venue in perpetual if markets.get(venue) != "linear")
    if spot_fed:
        raise ValueError(f"perpetual instruments on {spot_fed} need linear market feeds, not spot books")
    return _replace(settings, derivative_streams_enabled=True)


class LivePaperSession:
    def __init__(
        self,
        config_path: Path,
        output_dir: Path,
        session_id: str,
        settings: LiveFeedSettings | None = None,
        *,
        execution_mode: ExecutionMode = ExecutionMode.INTERNAL_PAPER,
        adapters: dict[str, VenueExecutionAdapter] | None = None,
        websocket_connector=public_websocket_connector,
        http_getter=public_http_get,
        clock_ns: Callable[[], int] = time.time_ns,
        publisher=None,
        status_poller: InstrumentStatusPoller | None = None,
        status_poll_seconds: float = 60.0,
        tick_seconds: float = 0.05,
        max_events_in_memory: int = 2_000_000,
        maker_simulator: Any | None = None,
        flatten_lead_seconds: float = 20.0,
        queue_simulation: dict | None = None,
        session_kwargs: dict | None = None,
        strategy_builder: Callable[["LivePaperSession"], list] | None = None,
        fee_readers: dict[str, Any] | None = None,
        require_account_fees: bool = False,
        journal_stream: bool = True,
        incident_capture: dict | None = None,
        session_limits: dict | None = None,
        clock_source: str | None = None,
        real_order_strategy: bool = False,
        real_order_problems: list | None = None,
    ) -> None:
        self.cfg, _ = load_config(Path(config_path))
        settings = settings or LiveFeedSettings(udp_enabled=False)
        settings = perpetual_feed_settings(self.cfg, settings)
        if set(settings.venues) != set(self.cfg.venues):
            raise ValueError("live paper trading requires every configured venue")
        # Owner 13 Nov (edge calibration item 2): a session that trades an account reads that account's own SOLUSDT maker/taker rate on every venue before anything starts and refuses to start if one differs from the rate
        # the profile books (or cannot be read). Real venue adapters are the readers by default; a paper session with credentials passes `fee_readers`; `require_account_fees` (the profile's oms.account_fee_check)
        # makes a missing reader a refusal too. A purely simulated session with none of these has no account to read and starts as before.
        readers = fee_readers if fee_readers is not None else (adapters if (adapters and execution_mode != ExecutionMode.INTERNAL_PAPER) else None)
        self.account_fee_check: list[dict] | None = None
        if require_account_fees or readers is not None or execution_mode != ExecutionMode.INTERNAL_PAPER:       # any mode that sends real orders reads the account's fees: no fallback to assumed fees
            self.account_fee_check = verify_account_fee_rates(self.cfg.instruments.fee_schedule, self.cfg.instrument_id, readers or {})
        # Owner 16 Nov (Order 3): the start-up checks of the session limits. Any mode that sends real orders (or has account readers) READS the account before anything starts and refuses to start when the
        # account is not as the profile expects (A8: one-way mode, margin mode, leverage, reduce-only accepted) or its own risk tiers cannot be read (A6: they replace the ASSUMED margin tiers); a real-order
        # session with a clock-offset gate (A3) refuses to start without a clock source. A purely simulated session with no account and no readers has nothing to read and starts as before (the checks are not run, and say so).
        if session_limits is None:
            session_limits = (session_kwargs or {}).get("session_limits")     # W2b: the profile setup carries the limits in the session arguments; the paper session's own start-up steps (arrival stamps, account checks) read the same block
        self.session_limits = session_limits
        if session_limits is not None and session_limits.get("feed_arrival_stamp"):
            settings = _dc_replace(settings, arrival_stamp=True)          # owner 17 Nov (A9 fix b): book frames stamped at arrival
        if incident_capture is not None:
            settings = _dc_replace(settings, incident_capture=incident_capture)      # W2b: the profile's incident_capture block (None, the default: nothing is built)
        if (session_kwargs or {}).get("feed_health") and (session_kwargs or {})["feed_health"].get("recorder_health_block"):
            settings = _dc_replace(settings, feed_health_block=True)      # W1: the recorder's part manifests carry the feed-health block
        self.account_checks: dict[str, Any] = {}
        co = (session_limits or {}).get("clock_offset") or {}
        self.clock_source = clock_source or co.get("source")           # Phase 2 F03 a: the profile's source ("chrony" in v1_17) unless the caller names one
        clock_source = self.clock_source
        self.chrony_every_ns = int(float(co.get("chrony_every_s", 60.0)) * 1e9)     # ASSUMED: chrony read every 60 s (a local read, no network)
        self.session_clock: MonotonicSessionClock | None = None
        if co.get("monotonic_clock") is not None and clock_ns is time.time_ns:
            # Phase 2 F03 b: intervals and deadlines on the monotonic clock (anchored once to the wall clock); an injected clock (tests) is left as it is
            self.session_clock = MonotonicSessionClock(step_threshold_ns=int(co["monotonic_clock"]["step_threshold_ms"] * 1e6))
            clock_ns = self.session_clock
        if session_limits is not None:
            real = readers is not None or execution_mode != ExecutionMode.INTERNAL_PAPER
            va = session_limits.get("verify_account")
            if va is not None and va.get("enabled") and real:
                self.account_checks["verify_account"] = verify_accounts_for_start(readers or {}, tuple(self.cfg.venues), self.cfg.instrument_id, margin_mode=va.get("margin_mode"), leverage=va.get("leverage"))
            if session_limits.get("tier_table") == "read_at_start" and real:
                tiers = read_account_tiers(readers or {}, tuple(self.cfg.venues), self.cfg.instrument_id)
                if self.cfg.margin is not None and self.cfg.margin.configured:
                    self.cfg = _dc_replace(self.cfg, margin=apply_account_tiers(self.cfg.margin, tiers, self.cfg.instrument_id))
                self.account_checks["tier_table"] = {v: {"source": t.source, "tiers": len(t.tiers), "last_max_notional": str(t.tiers[-1].max_notional)} for v, t in tiers.items()}
            if session_limits.get("account_tier_check") and real:
                # F07 (owner 20 Nov): read each account's EFFECTIVE rules (risk id, tier limit, leverage, margin mode, rates; UNKNOWN never defaulted) and refuse to start unless they match the tier table the
                # margin engine now uses at the session's hard cap. Kept apart from the fee snapshot.
                from .margin import check_account_tier, with_account_leverage
                rules, problems = {}, []
                from decimal import Decimal
                cap = Decimal(str(self.cfg.risk_limits.max_position_base))
                for venue in self.cfg.venues:
                    reader = (readers or {}).get(venue)
                    if reader is None or not hasattr(reader, "account_rules"):
                        problems.append(f"{venue}: no account rules reader")
                        continue
                    try:
                        rules[venue] = reader.account_rules(self.cfg.instrument_id)
                    except Exception as exc:
                        problems.append(f"{venue}: the account rules could not be read ({type(exc).__name__}: {exc})")
                        continue
                    schedule = self.cfg.margin.venues[venue].tiers.get(self.cfg.instrument_id) if self.cfg.margin is not None and venue in self.cfg.margin.venues else None
                    if schedule is not None and schedule.source.startswith("ACCOUNT_READ"):
                        # the account's own tiers were read (A6): the engine reserves at the ACCOUNT leverage, as the venue does
                        schedule = with_account_leverage(schedule, rules[venue])
                        venues = dict(self.cfg.margin.venues)
                        venues[venue] = _dc_replace(venues[venue], tiers={**venues[venue].tiers, self.cfg.instrument_id: schedule})
                        self.cfg = _dc_replace(self.cfg, margin=_dc_replace(self.cfg.margin, venues=venues))
                    problems += check_account_tier(rules[venue], schedule, cap)
                self.account_checks["account_rules"] = {v: {k: r[k] for k in r if k != "mark_price"} for v, r in rules.items()}
                if problems:
                    raise ValueError("the account's effective risk tier does not match the margin engine's tier table: " + "; ".join(problems))
            if session_limits.get("clock_offset") is not None and real and clock_source is None:
                raise ValueError("a session that sends real orders needs a clock source for the clock-offset gate (clock_source 'server_time' or 'chrony'); it refuses to start without one")
        self.clock_ns = clock_ns
        self.service = LiveOrderBookService(
            Path(config_path), Path(output_dir), session_id, settings,
            websocket_connector=websocket_connector, http_getter=http_getter,
            clock_ns=clock_ns, cancel_callback=self._cancel,
            venue_control_callback=self._venue_control, publisher=publisher,
            event_listener=self._on_event, pre_event_listener=self._before_event,
            trade_listener=self._on_trade,
            derivative_listener=self._on_derivative,
        )
        books: dict[tuple[str, str], OrderBook] = {
            (venue, self.cfg.instrument_id): book
            for venue, book in self.service.books.items()
        }
        # Phase D, D7b: the simulated venue behind paper trading is the Python queue simulator (queue v2) through `QueueAwareVenueAdapter`, fed per event by the shared `SimulatedVenueFeeder`; the C++
        # `maker_simulator` stays a separate, older evidence tool until the D7c comparison is reported. `queue_simulation` = {} turns it on with the executor's defaults, or holds `MakerConfig` overrides.
        self.feeder = None
        if queue_simulation is not None:
            if adapters is not None or execution_mode != ExecutionMode.INTERNAL_PAPER:
                raise ValueError("queue_simulation replaces the simulated venue adapters: INTERNAL_PAPER only, with no adapters supplied")
            from .simulation.queue_adapter import SimulatedVenueFeeder, build_queue_simulation
            adapters, self._views, self._states, self.queue_executor = build_queue_simulation(self.cfg, queue_simulation)
        kwargs = dict(session_kwargs or {})
        if session_limits is not None:
            kwargs.setdefault("session_limits", session_limits)
        self.session = TradingSession(
            self.cfg, execution_mode=execution_mode, live_data=True,
            adapters=adapters, books=books, clock_ns=clock_ns, **kwargs,
        )
        if queue_simulation is not None:
            # exchange-time eligibility stays off (ets None) until a measured host-clock offset exists (D7d); the venue names are the feed's own, so the name map is the identity
            self.feeder = SimulatedVenueFeeder(self.session, {v: v for v in self.cfg.venues}, self._views, self._states, adapters, ets_of=lambda ns: None)
        self.real_order_views = False
        if strategy_builder is not None:
            # Phase D, D7d (owner 9 Nov): the strategy of the profile (ArbSessionStrategy for v1_5), built by a caller-supplied builder that reads this session's own ledger, OMS, views and states: the same
            # strategy object the replay runs, in place of the default spread strategy. The core never imports the builder's package: it is handed in by name (cli `--strategy-setup`).
            if self.feeder is None:
                # (c), owner 17 Nov (out/integration/designs/C_real_order_session_design.md): with venue adapters the strategy's views come from the LIVE feed, through the same view builder the
                # simulated venue uses, and no simulated venue exists (the queue simulator's books stay replay and simulated-paper only). Real orders stay OFF: this needs an explicit flag that
                # nothing sets yet (the CLI refuses venue_testnet), and a profile that meets every real-order requirement.
                if adapters is None or execution_mode == ExecutionMode.INTERNAL_PAPER:
                    raise ValueError("a strategy builder needs queue_simulation (simulated paper) or venue adapters (a real-order session): it reads float views and venue states")
                if not real_order_strategy:
                    raise ValueError("a real-order strategy session is not enabled (real_order_strategy=False): see out/integration/designs/C_real_order_session_design.md; it is turned on with the testnet drill")
                if real_order_problems:
                    raise ValueError("the profile does not meet the real-order requirements: " + "; ".join(real_order_problems))
                from .simulation.queue_adapter import SimulatedVenueFeeder, build_live_views
                self._views, self._states = build_live_views(self.cfg)
                self.feeder = SimulatedVenueFeeder(self.session, {v: v for v in self.cfg.venues}, self._views, self._states, {}, ets_of=lambda ns: None)
                self.real_order_views = True
            built = list(strategy_builder(self))
            if not built:
                raise ValueError("the strategy builder returned no strategy")
            self.session.strategies[:] = built
            self.session.strategy = built[0]
        self.output_dir = self.service.recorder.session_dir / "paper"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._timestamps = (self.output_dir / "paper_timestamps.jsonl").open(
            "w", encoding="utf-8"
        )
        self.session.timestamp_sink = self._write_row
        self.settings = settings
        self._open_journals(journal_stream)
        self.events: list[MarketEvent] = []
        self.dropped_from_memory = 0
        self.max_events_in_memory = max_events_in_memory
        self.status_poller = status_poller
        self.status_poll_ns = int(status_poll_seconds * 1_000_000_000)
        self._next_status_poll_ns = 0
        self._next_clock_probe_ns = 0
        self._next_chrony_ns = 0
        self.clock_cross_check_alarm = False
        if self.session.clock_gate is not None and self.session_clock is not None:
            self.session.clock_gate.session_gap_of = self.session_clock.gap_ns
        self.tick_seconds = tick_seconds
        self.started_ts_ns: int | None = None
        self.settings = settings
        self.maker_simulator = maker_simulator
        self.flatten_lead_ns = int(flatten_lead_seconds * 1_000_000_000)
        self.flatten_started = False

    def _write_row(self, row: dict[str, Any]) -> None:
        self._timestamps.write(json.dumps(row, sort_keys=True, default=json_default) + "\n")
        if row.get("record_type") != "EVENT":
            self._timestamps.flush()                      # a decision, a status change, a failed probe: on disk (OS) at once. EVENT rows (one per market frame) follow the recorder's bounded flush
            self._journal_sync()
        else:
            self._event_rows_unflushed += 1
            if self._event_rows_unflushed >= self.settings.recorder_flush_writes or time.monotonic_ns() - self._journal_fsync_mono_ns >= self._journal_interval_ns:
                self._timestamps.flush()
                self._event_rows_unflushed = 0
                self._journal_sync()

    # ---- W2a (owner 17 Nov; audit A2 2.1): the session's own journals on disk AS THEY HAPPEN, so a killed process leaves every decision, order and intent row up to the kill.
    # DURABILITY POLICY, stated separately for the two kinds of file:
    #   recorder files (wire, raw, canonical, trace, trades; one or more rows per market frame): the A9 bounded flush: every `recorder_flush_writes` write calls or `recorder_flush_interval_seconds`,
    #     the monitor tick for a quiet feed, always at part close and shutdown. A hard kill can lose that window of the newest FRAMES.
    #   journals (ledger_stream.jsonl = every ledger event, orders and fills included; session_intent_journal.jsonl; the non-EVENT rows of paper_timestamps.jsonl): appended and FLUSHED PER RECORD
    #     (they are few: one per decision, order update or fill), so a hard kill of the process loses none of them. The EVENT rows of paper_timestamps.jsonl (one per market frame) follow the recorder's bound.
    #   fsync (power loss, not process death): only with `durable`, and for the journals at the same bounded interval, never per record (a per-record fsync costs milliseconds on this Mac).
    # `journal_manifest.json` is written at start with sealed false and rewritten at the normal end with sealed true and the counts: a recording whose manifest says sealed false was killed.
    def _open_journals(self, enabled: bool) -> None:
        self._event_rows_unflushed = 0
        self._journal_fsync_mono_ns = time.monotonic_ns()
        self._journal_interval_ns = int(self.settings.recorder_flush_interval_seconds * 1_000_000_000)
        self._journal_counts = {"ledger_events": 0, "intent_records": 0}
        self.journal_max_lag_ns = 0
        self._ledger_stream = self._intent_stream = None
        self.journal_stream = enabled
        incidents = getattr(self.service, "incidents", None)
        if incidents is not None:                             # W2b: a ledger event can raise an incident, and the window's journal rows are cut with the recording
            incidents.extra_flush = self._timestamps.flush
            incidents.extra_files = lambda: {name: self.output_dir / name for name in ("paper_timestamps.jsonl", "ledger_stream.jsonl", "session_intent_journal.jsonl")}
        if not enabled:
            if incidents is not None:
                self.session.ledger.event_sink = incidents.on_ledger_event
            return
        self._ledger_stream = (self.output_dir / "ledger_stream.jsonl").open("w", encoding="utf-8")
        for row in self.session.ledger.events:                # what the session recorded while it was built (opening balances)
            self._ledger_row(row)
        self.session.ledger.event_sink = self._ledger_row
        if self.session.intent_observer is not None:
            self._intent_stream = (self.output_dir / "session_intent_journal.jsonl").open("w", encoding="utf-8")
            self.session.intent_observer.layer.journal.sink = self._intent_row
        self._write_journal_manifest(sealed=False)

    def _ledger_row(self, row: dict[str, Any]) -> None:
        self._ledger_stream.write(json.dumps(row, sort_keys=True, default=json_default) + "\n")
        self._ledger_stream.flush()
        self._journal_counts["ledger_events"] += 1
        if self.service.incidents is not None:
            self.service.incidents.on_ledger_event(row)
        ts = row.get("ts_ns")
        if isinstance(ts, int) and ts > 10 ** 18:                 # a wall-clock stamp: how long after the event the row reached the file
            self.journal_max_lag_ns = max(self.journal_max_lag_ns, self.clock_ns() - ts)
        self._journal_sync()

    def _intent_row(self, row: dict[str, Any]) -> None:
        self._intent_stream.write(json.dumps(row, sort_keys=True, default=str) + "\n")
        self._intent_stream.flush()
        self._journal_counts["intent_records"] += 1
        self._journal_sync()

    def _journal_sync(self) -> None:
        if not self.settings.durable:
            return
        now = time.monotonic_ns()
        if now - self._journal_fsync_mono_ns < self._journal_interval_ns:
            return
        self._journal_fsync_mono_ns = now
        import os
        for handle in (self._timestamps, self._ledger_stream, self._intent_stream):
            if handle is not None and not handle.closed:
                handle.flush()
                os.fsync(handle.fileno())

    def _write_journal_manifest(self, sealed: bool) -> None:
        doc = {"sealed": sealed, "label": "sealed false = the process ended without finalize (killed): the journal files hold every record up to that moment",
               "policy": {"journals": "append and flush per record", "paper_timestamps EVENT rows": "the recorder's bounded flush", "fsync": "bounded interval, only with durable",
                          "recorder_flush_writes": self.settings.recorder_flush_writes, "recorder_flush_interval_seconds": self.settings.recorder_flush_interval_seconds, "durable": self.settings.durable},
               "files": ["paper_timestamps.jsonl", "ledger_stream.jsonl"] + (["session_intent_journal.jsonl"] if self._intent_stream is not None else []),
               "counts": dict(self._journal_counts), "journal_max_lag_ns": self.journal_max_lag_ns}
        (self.output_dir / "journal_manifest.json").write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")

    def _cancel(self, reason: str, now_ns: int) -> int:
        session = getattr(self, "session", None)
        return session.oms.cancel_open_orders(reason, now_ns) if session else 0

    def _venue_control(self, venue: str, valid: bool, reason: str, now_ns: int) -> int:
        session = getattr(self, "session", None)
        return session.oms.apply_feed_control(venue, valid, reason, now_ns) if session else 0

    def _before_event(self, event: MarketEvent) -> None:
        self.session.tick(self.clock_ns())

    def _on_event(
        self, event: MarketEvent, book: OrderBook, state: BookState, accepted: bool
    ) -> None:
        started = time.perf_counter_ns()
        now = self.clock_ns()
        if len(self.events) < self.max_events_in_memory:
            self.events.append(event)
        else:
            self.dropped_from_memory += 1
        if self.feeder is not None:
            self.feeder.on_book(event, apply_core=False, gate_allowed=not self.service.book_gate.suspended)
        else:
            self.session.on_event(
                event, now_ns=now, apply_book=False,
                gate_allowed=not self.service.book_gate.suspended,
            )
        if self.maker_simulator is not None:
            # Event time, not processing time: the same clock a replay uses.
            self.maker_simulator.on_book(event.venue, book, event.receive_ts_ns, event.event_id)
        self.session.stage_timer.add(
            "paper_frame_overhead_ns", time.perf_counter_ns() - started
        )
        self._write_row({
            "record_type": "EVENT", "event_id": event.event_id, "venue": event.venue,
            "exchange_ts_ns": event.exchange_ts_ns, "receive_ts_ns": event.receive_ts_ns,
            "processed_ts_ns": now, "book_state": state.value, "accepted": accepted,
        })

    def _on_derivative(self, ticker) -> None:
        session = getattr(self, "session", None)
        if session is not None:
            session.on_derivative(ticker)

    def _on_trade(self, tick) -> None:
        if self.feeder is not None:
            self.feeder.on_trade(tick)
        if self.maker_simulator is not None:
            self.maker_simulator.on_trade_tick(tick)

    async def _ticker(self) -> None:
        while not self.service.stop_event.is_set():
            await asyncio.sleep(self.tick_seconds)
            now = self.clock_ns()
            self.session.tick(now)
            if (
                not self.flatten_started
                and self.settings.max_runtime_seconds
                and self.started_ts_ns is not None
                and now - self.started_ts_ns
                >= int(self.settings.max_runtime_seconds * 1_000_000_000) - self.flatten_lead_ns
            ):
                self.flatten_started = True
                sent = self.session.enter_flatten(now)
                self._write_row({
                    "record_type": "FLATTEN_STARTED", "ts_ns": now, "exit_intents": sent,
                })
            if self.session_clock is not None:
                self._check_wall_step()
            if self.clock_source == "chrony" and now >= self._next_chrony_ns:
                await self._read_chrony(now)
            if self.clock_source in ("server_time", "chrony") and now >= self._next_clock_probe_ns:
                await self._probe_clock(now)
            if self.status_poller is None or now < self._next_status_poll_ns:
                continue
            self._next_status_poll_ns = now + self.status_poll_ns
            try:
                fetched = await asyncio.to_thread(self.status_poller.fetch)
                for change in self.status_poller.apply(fetched, self.clock_ns()):
                    self._write_row({"record_type": "INSTRUMENT_STATUS", **change})
            except Exception as exc:  # a failed poll must not stop trading data
                self._write_row({
                    "record_type": "INSTRUMENT_STATUS_POLL_FAILED",
                    "ts_ns": now, "error": f"{type(exc).__name__}: {exc}"[:300],
                })

    CLOCK_PROBE_SECONDS = 300.0                         # the ollog rule: the offset is re-measured every 300 s

    async def _probe_clock(self, now: int) -> None:
        """Owner 16 Nov (limit A3): measure the local clock against every venue's server time (five round trips each, the lowest wins) and hand the WORST venue to the gate (the conservative reading: both venues'
        stamps must be comparable). A venue whose probe fails leaves the gate with no fresh measurement, so it goes 'unknown' by age."""
        self._next_clock_probe_ns = now + int(self.CLOCK_PROBE_SECONDS * 1e9)
        gate = self.session.clock_gate
        found = [await probe_server_clock(self.service.http_getter, venue, clock_ns=self.clock_ns) for venue in self.cfg.venues]
        if any(m is None for m in found):
            self._write_row({"record_type": "CLOCK_PROBE_FAILED", "ts_ns": now, "venues": [v for v, m in zip(self.cfg.venues, found) if m is None]})
            return
        worst = max(found, key=lambda m: m.uncertainty_ns)
        if self.clock_source == "chrony":
            self._cross_check(now, found)                             # Phase 2 F03 a: under chrony the venues' server time is the cross-check only
            return
        # W1 (audit A1 1.6): the same measurement is the host clock monitors' independent reference (session and service), which until now were fed only by their own estimate
        self.session.time_monitor.record_clock_reference(worst)
        self.service.time_monitor.record_clock_reference(worst)
        if gate is None:
            return
        gate.update(ClockMeasurement(offset_ns=worst.offset_ns, uncertainty_ns=worst.uncertainty_ns, measured_ts_ns=max(m.measured_ts_ns for m in found), source="+".join(m.source for m in found), samples=sum(m.samples for m in found)))

    def _check_wall_step(self) -> None:
        """Phase 2 F03 b: a wall-clock step under the monotonic session clock is detected and recorded (row + ledger); the gate's offset carries the gap from then on."""
        ev = self.session_clock.check_step()
        if ev is not None:
            self._write_row({"record_type": "WALL_CLOCK_STEP", "ts_ns": ev["session_ts_ns"], **ev})
            self.session.ledger.record("WALL_CLOCK_STEP", ev["session_ts_ns"], alarm=True, step_ms=ev["step_ns"] / 1e6, gap_after_ms=ev["gap_after_ns"] / 1e6, threshold_ms=ev["threshold_ns"] / 1e6)

    async def _read_chrony(self, now: int) -> None:
        """Phase 2 F03 a / c: read chrony; a synchronised reading updates the gate (and the host clock monitors' reference); an absent, failing or unsynchronised chrony INVALIDATES the gate ("unknown",
        quoting stops): no fallback. One CLOCK_MEASUREMENT row per reading, with both numbers and every chrony field."""
        self._next_chrony_ns = now + self.chrony_every_ns
        m, fields, why = await asyncio.to_thread(read_chrony)
        gate = self.session.clock_gate
        if m is None:
            self._write_row({"record_type": "CLOCK_SOURCE_UNAVAILABLE", "ts_ns": now, "source": "chrony", "reason": why, "fields": fields})
            if gate is not None:
                gate.invalidate(f"chrony: {why}")
            return
        self.session.time_monitor.record_clock_reference(m)
        self.service.time_monitor.record_clock_reference(m)
        if gate is not None:
            gate.update(m)
        state = gate.evaluate(now)[0] if gate is not None else None
        self._write_row({"record_type": "CLOCK_MEASUREMENT", "ts_ns": now, "source": m.source, "offset_ms": m.offset_ns / 1e6, "uncertainty_ms": m.uncertainty_ns / 1e6,
                         "checked_offset_ms": None if gate is None or gate.checked_offset_ns() is None else gate.checked_offset_ns() / 1e6, "gate_state": state, "fields": fields})

    def _cross_check(self, now: int, found: list) -> None:
        """Phase 2 F03 a: the venues' server time against chrony. A venue whose offset differs from chrony's by more than its own probe uncertainty (half its round trip) raises ONE
        CLOCK_CROSS_CHECK_DISAGREE alarm per episode (cleared when every venue agrees again). It does not stop quoting."""
        gate = self.session.clock_gate
        ref = None if gate is None else gate.measurement
        rows = [{"venue": v, "server_offset_ms": m.offset_ns / 1e6, "server_uncertainty_ms": m.uncertainty_ns / 1e6} for v, m in zip(self.cfg.venues, found)]
        if ref is None:
            self._write_row({"record_type": "CLOCK_CROSS_CHECK", "ts_ns": now, "chrony": None, "venues": rows})
            return
        bad = [r["venue"] for r, m in zip(rows, found) if abs(m.offset_ns - ref.offset_ns) > m.uncertainty_ns]
        for r, m in zip(rows, found):
            r["minus_chrony_ms"] = (m.offset_ns - ref.offset_ns) / 1e6
        self._write_row({"record_type": "CLOCK_CROSS_CHECK", "ts_ns": now, "chrony_offset_ms": ref.offset_ns / 1e6, "venues": rows, "disagree": bad})
        if bad and not self.clock_cross_check_alarm:
            self.clock_cross_check_alarm = True
            self.session.ledger.record("CLOCK_CROSS_CHECK_DISAGREE", now, alarm=True, venues=",".join(bad), rows=rows)
        elif not bad and self.clock_cross_check_alarm:
            self.clock_cross_check_alarm = False
            self.session.ledger.record("CLOCK_CROSS_CHECK_AGREE", now)

    async def run(self) -> dict[str, Any]:
        self.started_ts_ns = self.clock_ns()
        if self.clock_source == "chrony":
            await self._read_chrony(self.started_ts_ns)          # quoting starts only after a measurement exists (chrony absent: "unknown", no quote)
        if self.clock_source in ("server_time", "chrony"):
            await self._probe_clock(self.started_ts_ns)         # quoting starts only after a measurement exists
        ticker = asyncio.create_task(self._ticker())
        try:
            service_summary = await self.service.run()
        finally:
            self.service.request_stop()
            ticker.cancel()
            with suppress(asyncio.CancelledError):
                await ticker
        return self.finalize(service_summary)

    def finalize(self, service_summary: dict[str, Any]) -> dict[str, Any]:
        stopped = self.clock_ns()
        self.session.oms.drain(
            max(stopped, self.session.last_event_ts_ns) + self.cfg.drain_horizon_ns
        )
        self.session.finalize_funding()
        self._timestamps.close()
        if self.journal_stream:
            self.session.ledger.event_sink = None
            for handle in (self._ledger_stream, self._intent_stream):
                if handle is not None:
                    handle.close()
            if self.session.intent_observer is not None:
                self.session.intent_observer.layer.journal.sink = None
            self._write_journal_manifest(sealed=True)
        canonical = self.output_dir / "canonical_events.jsonl"
        with canonical.open("w", encoding="utf-8") as handle:
            # processing_ordinal: the order this session applied its events, which a
            # replay reproduces (replay.replay_order); receive time alone can differ.
            for ordinal, event in enumerate(self.events, start=1):
                row = {**asdict(event), "processing_ordinal": ordinal}
                handle.write(json.dumps(row, default=json_default, sort_keys=True) + "\n")
        timings = [
            row for adapter in self.session.router.adapters.values()
            for row in getattr(adapter, "timings", ())
        ]
        if timings:
            (self.output_dir / "testnet_order_timestamps.jsonl").write_text(
                "".join(json.dumps(row, sort_keys=True) + "\n" for row in timings),
                encoding="utf-8",
            )
        if not self.events:
            result = {
                "status": "NO_MARKET_DATA",
                "execution_mode": self.session.execution_mode.value,
                "live_feed": service_summary,
            }
            (self.output_dir / "paper_summary.json").write_text(
                json.dumps(result, indent=2, sort_keys=True, default=str), encoding="utf-8"
            )
            return result
        stats = list(self.service.stats.values())
        internal = self.session.execution_mode == ExecutionMode.INTERNAL_PAPER
        summary = write_session_artifacts(
            self.session, events=self.events, output_dir=self.output_dir,
            correlation_id=self.service.recorder.session_id,
            source_path=str(canonical), source_sha256=file_sha256(canonical),
            replay_duration_ns=max(1, stopped - (self.started_ts_ns or stopped)),
            canonical_events_delivered=len(self.events),
            scope="LIVE_PAPER_SESSION", monitor_mode="LIVE",
            job_mode=self.session.execution_mode.value, clock_label="HOST_WALL_CLOCK",
            live_metrics={
                "reconnects": sum(item.reconnects for item in stats),
                "stale_disconnects": sum(item.stale_disconnects for item in stats),
                "persistence_lag_ns": self.service.recorder.max_persistence_lag_ns,
            },
            invalid_trades=sum(item.invalid_trades for item in stats),
            limitations=(
                (
                    "INTERNAL_PAPER: fills are simulated against live public books "
                    "with the configured latency model; no venue order was sent."
                ) if internal else (
                    "VENUE_TESTNET: orders were sent to venue testnets, whose "
                    "liquidity and matching differ from production."
                ),
                "Receive latency is measured on the host clock after offset correction.",
            ),
        )
        summary["live_feed"] = service_summary
        # W1 (audit A1 1.8): in a live session the SERVICE's gate acts (it passes its verdict to the session, whose own gate is never evaluated), so the block above read INITIAL_SYNC with 0 transitions
        # while quoting had been suspended many times. The acting gate is reported beside it and the session's block is labelled.
        if isinstance(summary.get("book_integrity_gate"), dict):
            summary["book_integrity_gate"]["label"] = "the session's own gate: NOT evaluated in a live session (the feed service passes its verdict in); see book_integrity_gate_acting"
        summary["book_integrity_gate_acting"] = {"source": "live feed service (strategy_gate)", "suspended": self.service.book_gate.suspended, "reason": self.service.book_gate.reason,
                                                 "suspend_count": self.service.book_gate.suspend_count, "resume_count": self.service.book_gate.resume_count,
                                                 "cancel_requests": self.service.book_gate.cancel_requests, "cancelled_orders": self.service.book_gate.cancelled_orders}
        if self.session.feed_health is not None:
            summary["feed_health"] = self.session.feed_health_report(stopped)
        summary["paper_timestamps"] = "paper_timestamps.jsonl"
        summary["events_dropped_from_memory"] = self.dropped_from_memory
        summary["flatten_started"] = self.flatten_started
        if not internal:
            # F04 item 5 (owner 20 Nov): every real-order session ends with the amend journal audit; a non-zero two-live-orders / booked-twice / overfill count is one REAL_ORDER_AUDIT_ALARM
            from .session_audit import real_order_audit
            summary["real_order_audit"] = real_order_audit(self.session.ledger, self.session.oms.plans, stopped, self.session.oms.alert_context(stopped))
        if getattr(self.session.oms, "dust_aggregate", False):
            # Phase 2 F06 (owner 20 Nov): the operator's end-of-session view of the carried dust: every balance left is an OPEN ITEM (venue, symbol, signed base, USDT, age); no automatic order
            ag = self.session.oms.dust_agg_stats
            summary["operator_open_items"] = {"carried_dust": self.session.oms.dust_open_items(stopped), "dust_age_alarms": ag["age_alarms"],
                                              "dust_bound_active_at_end": self.session.oms.dust_bound is not None, "max_aggregate_dust_usdt": str(ag["max_aggregate_usdt"])}
        if self.account_fee_check is not None:
            summary["account_fee_snapshot"] = self.account_fee_check            # F07 (owner 20 Nov): the fee-rate snapshot and the risk-tier snapshot are kept apart
        if self.account_checks:
            summary["account_risk_snapshot"] = self.account_checks
        if self.maker_simulator is not None:
            self.maker_simulator.finish()  # end at the last event, as a replay does
            maker = self.maker_simulator.report()
            maker["fills_path"] = self.maker_simulator.write_fills(
                self.output_dir / "maker_dry_run_fills.jsonl"
            ).name
            summary["maker_dry_run"] = maker
            (self.output_dir / "maker_dry_run.json").write_text(
                json.dumps(maker, indent=2, sort_keys=True), encoding="utf-8"
            )
        (self.output_dir / "paper_summary.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True, default=str), encoding="utf-8"
        )
        return summary
