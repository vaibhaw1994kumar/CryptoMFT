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
from dataclasses import asdict
import json
from pathlib import Path
import time
from typing import Any, Callable

from .config import load_config
from .execution import VenueExecutionAdapter
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
    ) -> None:
        self.cfg, _ = load_config(Path(config_path))
        settings = settings or LiveFeedSettings(udp_enabled=False)
        if set(settings.venues) != set(self.cfg.venues):
            raise ValueError("live paper trading requires every configured venue")
        self.clock_ns = clock_ns
        self.service = LiveOrderBookService(
            Path(config_path), Path(output_dir), session_id, settings,
            websocket_connector=websocket_connector, http_getter=http_getter,
            clock_ns=clock_ns, cancel_callback=self._cancel,
            venue_control_callback=self._venue_control, publisher=publisher,
            event_listener=self._on_event, pre_event_listener=self._before_event,
            trade_listener=self._on_trade,
        )
        books: dict[tuple[str, str], OrderBook] = {
            (venue, self.cfg.instrument_id): book
            for venue, book in self.service.books.items()
        }
        self.session = TradingSession(
            self.cfg, execution_mode=execution_mode, live_data=True,
            adapters=adapters, books=books, clock_ns=clock_ns,
        )
        self.output_dir = self.service.recorder.session_dir / "paper"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._timestamps = (self.output_dir / "paper_timestamps.jsonl").open(
            "w", encoding="utf-8"
        )
        self.session.timestamp_sink = self._write_row
        self.events: list[MarketEvent] = []
        self.dropped_from_memory = 0
        self.max_events_in_memory = max_events_in_memory
        self.status_poller = status_poller
        self.status_poll_ns = int(status_poll_seconds * 1_000_000_000)
        self._next_status_poll_ns = 0
        self.tick_seconds = tick_seconds
        self.started_ts_ns: int | None = None
        self.settings = settings
        self.maker_simulator = maker_simulator
        self.flatten_lead_ns = int(flatten_lead_seconds * 1_000_000_000)
        self.flatten_started = False

    def _write_row(self, row: dict[str, Any]) -> None:
        self._timestamps.write(json.dumps(row, sort_keys=True, default=json_default) + "\n")

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
        self.session.on_event(
            event, now_ns=now, apply_book=False,
            gate_allowed=not self.service.book_gate.suspended,
        )
        if self.maker_simulator is not None:
            self.maker_simulator.on_book(event.venue, book, now)
        self.session.stage_timer.add(
            "paper_frame_overhead_ns", time.perf_counter_ns() - started
        )
        self._write_row({
            "record_type": "EVENT", "event_id": event.event_id, "venue": event.venue,
            "exchange_ts_ns": event.exchange_ts_ns, "receive_ts_ns": event.receive_ts_ns,
            "processed_ts_ns": now, "book_state": state.value, "accepted": accepted,
        })

    def _on_trade(self, tick) -> None:
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

    async def run(self) -> dict[str, Any]:
        self.started_ts_ns = self.clock_ns()
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
        self._timestamps.close()
        canonical = self.output_dir / "canonical_events.jsonl"
        with canonical.open("w", encoding="utf-8") as handle:
            for event in self.events:
                handle.write(json.dumps(asdict(event), default=json_default, sort_keys=True) + "\n")
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
        summary["paper_timestamps"] = "paper_timestamps.jsonl"
        summary["events_dropped_from_memory"] = self.dropped_from_memory
        summary["flatten_started"] = self.flatten_started
        if self.maker_simulator is not None:
            maker = self.maker_simulator.report()
            summary["maker_dry_run"] = maker
            (self.output_dir / "maker_dry_run.json").write_text(
                json.dumps(maker, indent=2, sort_keys=True), encoding="utf-8"
            )
        (self.output_dir / "paper_summary.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True, default=str), encoding="utf-8"
        )
        return summary
