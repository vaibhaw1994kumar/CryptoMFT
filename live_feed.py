from __future__ import annotations

from decimal import Decimal

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, suppress
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import logging
import os
from pathlib import Path
import time
from typing import Any, Protocol
from urllib.request import Request, urlopen

from .adapters import normalize_binance_depth, normalize_bybit_depth
from .book_gate import BookIntegrityGate, CancelCallback, VenueControlCallback
from .config import load_config
from .models import BookState, MarketEvent, json_default
from .market_data_output import (
    LtpOhlcBar,
    LtpOhlcCsvWriter,
    LtpOhlcResampler,
    MarketDataPublisher,
    OhlcBar,
    OhlcCsvWriter,
    OrderBookOhlcResampler,
    TradeTick,
    UdpMulticastPublisher,
    UdpPublisherSettings,
    normalize_binance_trade,
    normalize_bybit_trades,
)
from .orderbook import OrderBook
from .raw_capture import GENESIS_HASH, RawCaptureWriter, RawFrame
from .monitoring import PipelineMonitor
from .structured_logging import StructuredLogLevel, StructuredLogStore


LOGGER = logging.getLogger(__name__)
WIRE_SCHEMA_VERSION = "1.0.0"


class WebSocketLike(Protocol):
    async def recv(self) -> str | bytes: ...

    async def send(self, message: str | bytes) -> None: ...


WebSocketConnector = Callable[
    [str, str], AbstractAsyncContextManager[WebSocketLike]
]
HttpGetter = Callable[[str, float], Awaitable[tuple[str | bytes, int]]]


class ResyncRequired(RuntimeError):
    """The connection must restart so a clean snapshot can be acquired."""


@dataclass(frozen=True, slots=True)
class WireFrame:
    wire_ref: str
    wire_schema_version: str
    venue: str
    channel: str
    host: str
    transport: str
    receive_ts_ns: int
    frame_encoding: str
    frame_data: str
    frame_sha256: str
    previous_record_hash: str
    record_hash: str


@dataclass(slots=True)
class VenueLiveStats:
    connection_attempts: int = 0
    connections: int = 0
    disconnects: int = 0
    reconnects: int = 0
    resyncs: int = 0
    stale_disconnects: int = 0
    raw_frames: int = 0
    canonical_events: int = 0
    control_frames: int = 0
    parse_errors: int = 0
    normalization_errors: int = 0
    trade_connection_attempts: int = 0
    trade_connections: int = 0
    trade_disconnects: int = 0
    trade_reconnects: int = 0
    trade_raw_frames: int = 0
    trade_ticks: int = 0
    trade_parse_errors: int = 0
    trade_normalization_errors: int = 0
    trade_stale_disconnects: int = 0
    invalid_trades: int = 0
    last_error: str = ""


@dataclass(frozen=True, slots=True)
class LiveFeedSettings:
    venues: tuple[str, ...] = ("BINANCE", "BYBIT")
    binance_market: str = "spot"
    bybit_market: str = "spot"
    binance_ws_base: str | None = None
    binance_rest_base: str | None = None
    bybit_ws_url: str | None = None
    bybit_depth: int = 50
    http_timeout_seconds: float = 10.0
    initial_sync_timeout_seconds: float = 15.0
    reconnect_min_seconds: float = 1.0
    reconnect_max_seconds: float = 30.0
    rotate_seconds: float = 3600.0
    rotate_bytes: int = 256 * 1024 * 1024
    durable: bool = True
    max_runtime_seconds: float | None = None
    max_events: int | None = None
    ohlc_interval_seconds: int = 60
    trade_streams_enabled: bool = False
    trade_stale_seconds: float = 30.0
    udp_enabled: bool = False
    udp_group: str = "239.255.77.77"
    udp_port: int = 19_001
    udp_interface: str = "0.0.0.0"
    udp_ttl: int = 1
    udp_depth: int = 20
    udp_max_datagram_bytes: int = 60_000
    monitor_interval_seconds: float = 1.0
    max_trade_deviation_bps: float = 500.0

    def validate(self) -> None:
        if not self.venues:
            raise ValueError("at least one venue is required")
        if any(x not in ("BINANCE", "BYBIT") for x in self.venues):
            raise ValueError("venues must be BINANCE and/or BYBIT")
        if self.binance_market not in ("spot", "linear"):
            raise ValueError("binance_market must be spot or linear")
        if self.bybit_market not in ("spot", "linear"):
            raise ValueError("bybit_market must be spot or linear")
        if self.bybit_depth not in (1, 50, 200, 1000):
            raise ValueError("Bybit depth must be one of 1, 50, 200 or 1000")
        if self.http_timeout_seconds <= 0:
            raise ValueError("http_timeout_seconds must be positive")
        if self.initial_sync_timeout_seconds <= 0:
            raise ValueError("initial_sync_timeout_seconds must be positive")
        if self.reconnect_min_seconds <= 0:
            raise ValueError("reconnect_min_seconds must be positive")
        if self.reconnect_max_seconds < self.reconnect_min_seconds:
            raise ValueError("reconnect_max_seconds must be >= reconnect_min_seconds")
        if self.rotate_seconds <= 0 or self.rotate_bytes <= 0:
            raise ValueError("rotation thresholds must be positive")
        if self.max_runtime_seconds is not None and self.max_runtime_seconds <= 0:
            raise ValueError("max_runtime_seconds must be positive")
        if self.max_events is not None and self.max_events <= 0:
            raise ValueError("max_events must be positive")
        if self.ohlc_interval_seconds <= 0:
            raise ValueError("ohlc_interval_seconds must be positive")
        if self.trade_stale_seconds <= 0:
            raise ValueError("trade_stale_seconds must be positive")
        if self.monitor_interval_seconds <= 0:
            raise ValueError("monitor_interval_seconds must be positive")
        if self.udp_enabled:
            UdpPublisherSettings(
                group=self.udp_group,
                port=self.udp_port,
                interface=self.udp_interface,
                ttl=self.udp_ttl,
                depth=self.udp_depth,
                max_datagram_bytes=self.udp_max_datagram_bytes,
            ).validate()


class RotatingLiveRecorder:
    """Raw-first rotating wire/raw/canonical recorder for a live session."""

    def __init__(
        self,
        output_dir: Path,
        session_id: str,
        *,
        rotate_seconds: float,
        rotate_bytes: int,
        durable: bool,
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        self.output_dir = output_dir
        self.session_id = session_id
        self.session_dir = output_dir / session_id
        if self.session_dir.exists() and any(self.session_dir.iterdir()):
            raise FileExistsError(
                f"live session already exists and will not be overwritten: {self.session_dir}"
            )
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.rotate_ns = int(rotate_seconds * 1_000_000_000)
        self.rotate_bytes = rotate_bytes
        self.durable = durable
        self.clock_ns = clock_ns
        self.part_number = 0
        self.part_started_ns = 0
        self.wire_count = 0
        self.canonical_count = 0
        self.trade_count = 0
        self.rejected_count = 0
        self.control_count = 0
        self.last_persisted_receive_ts_ns: int | None = None
        self.max_persistence_lag_ns = 0
        self.parts: list[dict[str, Any]] = []
        self._wire_previous_hash = GENESIS_HASH
        self._wire_ordinal = 0
        self._raw_writer: RawCaptureWriter | None = None
        self._wire_handle: Any = None
        self._canonical_handle: Any = None
        self._trade_handle: Any = None
        self._trace_handle: Any = None
        self._quarantine_handle: Any = None
        self._part_dir: Path | None = None
        self._closed = False

    @staticmethod
    def _canonical_json(value: Any) -> str:
        return json.dumps(
            value, default=json_default, sort_keys=True, separators=(",", ":")
        )

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def _flush(self, handle: Any) -> None:
        handle.flush()
        if self.durable:
            os.fsync(handle.fileno())

    def _open_part(self, now_ns: int) -> None:
        self.part_number += 1
        part_name = f"part-{self.part_number:06d}"
        relative_session = f"{self.session_id}/{part_name}"
        self._raw_writer = RawCaptureWriter(
            self.output_dir, relative_session, durable=self.durable
        )
        self._part_dir = self.session_dir / part_name
        self._wire_handle = (self._part_dir / "wire.jsonl").open("x", encoding="utf-8")
        self._canonical_handle = (self._part_dir / "canonical.jsonl").open(
            "x", encoding="utf-8"
        )
        self._trade_handle = (self._part_dir / "trades.jsonl").open(
            "x", encoding="utf-8"
        )
        self._trace_handle = (self._part_dir / "trace.jsonl").open("x", encoding="utf-8")
        self._quarantine_handle = (self._part_dir / "quarantine.jsonl").open(
            "x", encoding="utf-8"
        )
        self.part_started_ns = now_ns
        self._wire_previous_hash = GENESIS_HASH
        self._wire_ordinal = 0

    def _should_rotate(self, now_ns: int) -> bool:
        if self._raw_writer is None:
            return False
        elapsed = now_ns - self.part_started_ns >= self.rotate_ns
        size = (
            self._raw_writer.raw_path.stat().st_size
            + (self._part_dir / "wire.jsonl").stat().st_size
            + (self._part_dir / "canonical.jsonl").stat().st_size
            + (self._part_dir / "trades.jsonl").stat().st_size
        )
        return elapsed or size >= self.rotate_bytes

    def _ensure_part(self, now_ns: int) -> None:
        if self._raw_writer is None:
            self._open_part(now_ns)
        elif self._should_rotate(now_ns):
            self._close_part()
            self._open_part(now_ns)

    def capture_wire(
        self,
        *,
        venue: str,
        channel: str,
        host: str,
        transport: str,
        receive_ts_ns: int,
        original_frame: str | bytes,
    ) -> WireFrame:
        if self._closed:
            raise RuntimeError("live recorder is closed")
        self._ensure_part(receive_ts_ns)
        if isinstance(original_frame, bytes):
            import base64

            frame_bytes = original_frame
            frame_data = base64.b64encode(original_frame).decode("ascii")
            encoding = "BASE64"
        else:
            frame_bytes = original_frame.encode("utf-8")
            frame_data = original_frame
            encoding = "UTF8"
        frame_sha256 = sha256(frame_bytes).hexdigest()
        self._wire_ordinal += 1
        unsigned = {
            "wire_ref": sha256(
                f"{self.session_id}|{self.part_number}|{self._wire_ordinal}|{venue}|"
                f"{channel}|{receive_ts_ns}|{frame_sha256}".encode()
            ).hexdigest(),
            "wire_schema_version": WIRE_SCHEMA_VERSION,
            "venue": venue,
            "channel": channel,
            "host": host,
            "transport": transport,
            "receive_ts_ns": receive_ts_ns,
            "frame_encoding": encoding,
            "frame_data": frame_data,
            "frame_sha256": frame_sha256,
            "previous_record_hash": self._wire_previous_hash,
        }
        record_hash = sha256(self._canonical_json(unsigned).encode()).hexdigest()
        wire = WireFrame(record_hash=record_hash, **unsigned)
        self._wire_handle.write(self._canonical_json(asdict(wire)) + "\n")
        self._flush(self._wire_handle)
        self._wire_previous_hash = record_hash
        self.wire_count += 1
        return wire

    def append_payload(
        self,
        wire: WireFrame,
        *,
        payload: dict[str, Any],
        vendor: str,
        adapter_version: str,
    ) -> RawFrame:
        if self._raw_writer is None:
            raise RuntimeError("wire frame has no active part")
        if wire.frame_encoding == "BASE64":
            import base64

            original_frame: str | bytes = base64.b64decode(wire.frame_data)
        else:
            original_frame = wire.frame_data
        return self._raw_writer.append(
            venue=wire.venue,
            vendor=vendor,
            channel=wire.channel,
            host=wire.host,
            receive_ts_ns=wire.receive_ts_ns,
            payload=payload,
            adapter_version=adapter_version,
            original_frame=original_frame,
            transport=wire.transport,
            wire_ref=wire.wire_ref,
        )

    def record_event(self, wire: WireFrame, raw: RawFrame, event: MarketEvent) -> None:
        self._canonical_handle.write(
            json.dumps(asdict(event), default=json_default, sort_keys=True) + "\n"
        )
        self._trace_handle.write(self._canonical_json({
            "wire_ref": wire.wire_ref,
            "raw_ref": raw.raw_ref,
            "canonical_event_id": event.event_id,
            "venue": event.venue,
            "instrument_id": event.instrument_id,
            "sequence_start": event.sequence_start,
            "sequence": event.sequence,
            "receive_ts_ns": event.receive_ts_ns,
            "disposition": "CANONICAL",
        }) + "\n")
        self._flush(self._canonical_handle)
        self._flush(self._trace_handle)
        persisted_ns = self.clock_ns()
        self.last_persisted_receive_ts_ns = event.receive_ts_ns
        self.max_persistence_lag_ns = max(
            self.max_persistence_lag_ns, max(0, persisted_ns - event.receive_ts_ns)
        )
        self.canonical_count += 1

    def record_trade(self, wire: WireFrame, raw: RawFrame, tick: TradeTick) -> None:
        self._trade_handle.write(self._canonical_json(tick.to_dict()) + "\n")
        self._trace_handle.write(self._canonical_json({
            "wire_ref": wire.wire_ref,
            "raw_ref": raw.raw_ref,
            "canonical_event_id": tick.event_id,
            "venue": tick.venue,
            "instrument_id": tick.instrument_id,
            "trade_id": tick.trade_id,
            "receive_ts_ns": tick.receive_ts_ns,
            "disposition": "TRADE",
        }) + "\n")
        self._flush(self._trade_handle)
        self._flush(self._trace_handle)
        persisted_ns = self.clock_ns()
        self.last_persisted_receive_ts_ns = tick.receive_ts_ns
        self.max_persistence_lag_ns = max(
            self.max_persistence_lag_ns, max(0, persisted_ns - tick.receive_ts_ns)
        )
        self.trade_count += 1

    def record_control(self, wire: WireFrame, raw: RawFrame) -> None:
        self._trace_handle.write(self._canonical_json({
            "wire_ref": wire.wire_ref,
            "raw_ref": raw.raw_ref,
            "canonical_event_id": None,
            "venue": raw.venue,
            "receive_ts_ns": raw.receive_ts_ns,
            "disposition": "CONTROL",
        }) + "\n")
        self._flush(self._trace_handle)
        self.control_count += 1

    def record_rejection(
        self,
        wire: WireFrame,
        error: Exception,
        *,
        raw_ref: str = "",
        stage: str,
    ) -> None:
        self._quarantine_handle.write(self._canonical_json({
            "wire_ref": wire.wire_ref,
            "raw_ref": raw_ref,
            "venue": wire.venue,
            "receive_ts_ns": wire.receive_ts_ns,
            "stage": stage,
            "error_type": type(error).__name__,
            "error": str(error),
        }) + "\n")
        self._flush(self._quarantine_handle)
        self.rejected_count += 1

    def _close_part(self) -> None:
        if self._raw_writer is None or self._part_dir is None:
            return
        for handle in (
            self._wire_handle,
            self._canonical_handle,
            self._trade_handle,
            self._trace_handle,
            self._quarantine_handle,
        ):
            self._flush(handle)
            handle.close()
        raw_manifest = self._raw_writer.close()
        paths = {
            "wire": self._part_dir / "wire.jsonl",
            "raw": self._part_dir / "raw.jsonl",
            "canonical": self._part_dir / "canonical.jsonl",
            "trades": self._part_dir / "trades.jsonl",
            "trace": self._part_dir / "trace.jsonl",
            "quarantine": self._part_dir / "quarantine.jsonl",
        }
        part = {
            "part": self.part_number,
            "directory": str(self._part_dir),
            "started_ts_ns": self.part_started_ns,
            "closed_ts_ns": self.clock_ns(),
            "raw_manifest": raw_manifest,
            "files": {
                name: {"path": str(path), "sha256": self._file_sha256(path)}
                for name, path in paths.items()
            },
            "terminal_wire_record_hash": self._wire_previous_hash,
        }
        (self._part_dir / "live_part_manifest.json").write_text(
            json.dumps(part, indent=2, sort_keys=True), encoding="utf-8"
        )
        self.parts.append(part)
        self._raw_writer = None
        self._part_dir = None

    def close(self, service_summary: dict[str, Any]) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("live recorder already closed")
        self._close_part()
        self._closed = True
        stable = {
            "session_id": self.session_id,
            "wire_records": self.wire_count,
            "canonical_events": self.canonical_count,
            "trade_ticks": self.trade_count,
            "control_frames": self.control_count,
            "rejected_frames": self.rejected_count,
            "parts": [
                {
                    "part": x["part"],
                    "files": x["files"],
                    "terminal_wire_record_hash": x["terminal_wire_record_hash"],
                    "terminal_raw_record_hash": x["raw_manifest"]["terminal_record_hash"],
                }
                for x in self.parts
            ],
            "service": service_summary,
        }
        manifest = {
            "schema_version": "1.0.0",
            "closed_at_utc": datetime.now(timezone.utc).isoformat(),
            **stable,
            "session_sha256": sha256(self._canonical_json(stable).encode()).hexdigest(),
        }
        manifest_path = self.session_dir / "live_manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
        )
        manifest["manifest_path"] = str(manifest_path)
        return manifest


async def public_http_get(url: str, timeout_seconds: float) -> tuple[str | bytes, int]:
    def fetch() -> tuple[bytes, int]:
        request = Request(url, headers={"User-Agent": "crypto-stat-arb-platform/0.1"})
        with urlopen(request, timeout=timeout_seconds) as response:
            body = response.read()
        return body, time.time_ns()

    body, receive_ts_ns = await asyncio.to_thread(fetch)
    return body, receive_ts_ns


def public_websocket_connector(
    venue: str, url: str
) -> AbstractAsyncContextManager[WebSocketLike]:
    del venue
    try:
        from websockets.asyncio.client import connect
    except ImportError:
        from websockets import connect  # type: ignore[no-redef]
    return connect(
        url,
        ping_interval=20,
        ping_timeout=20,
        # Short on purpose: during shutdown we do not need a clean close
        # handshake to complete, only to stop waiting for one quickly. A
        # long close_timeout was one contributor to cycles taking far
        # longer than --max-runtime-seconds to actually exit under
        # sustained reconnect/DNS-outage conditions.
        close_timeout=2,
        max_queue=4096,
        open_timeout=10,
        # Race every resolved address (RFC 8305) instead of trying them one by
        # one: on hosts without an IPv6 route, or when some CDN edges
        # black-hole, sequential attempts burn the whole open timeout.
        happy_eyeballs_delay=0.25,
        interleave=1,
    )


class LiveOrderBookService:
    """Continuously capture and reconstruct one configured instrument per venue."""

    def __init__(
        self,
        config_path: Path,
        output_dir: Path,
        session_id: str,
        settings: LiveFeedSettings | None = None,
        *,
        websocket_connector: WebSocketConnector = public_websocket_connector,
        http_getter: HttpGetter = public_http_get,
        clock_ns: Callable[[], int] = time.time_ns,
        cancel_callback: CancelCallback | None = None,
        venue_control_callback: VenueControlCallback | None = None,
        publisher: MarketDataPublisher | None = None,
        event_listener: Callable[[MarketEvent, OrderBook, BookState, bool], None] | None = None,
        trade_listener: Callable[[TradeTick], None] | None = None,
        pre_event_listener: Callable[[MarketEvent], None] | None = None,
    ) -> None:
        self.pre_event_listener = pre_event_listener
        self.config, _ = load_config(config_path)
        self.event_listener = event_listener
        self.trade_listener = trade_listener
        self.settings = settings or LiveFeedSettings()
        self.settings.validate()
        configured = set(self.config.venues)
        if not set(self.settings.venues).issubset(configured):
            raise ValueError(
                f"requested venues must be configured; configured={sorted(configured)}"
            )
        self.websocket_connector = websocket_connector
        self.http_getter = http_getter
        self.clock_ns = clock_ns
        self.stop_event = asyncio.Event()
        self.started_ts_ns: int | None = None
        self.stopped_ts_ns: int | None = None
        self.events_seen = 0
        self.books = {
            venue: self.config.book_sanity.new_book(venue, self.config.instrument_id)
            for venue in self.settings.venues
        }
        self.stats = {venue: VenueLiveStats() for venue in self.settings.venues}
        self.book_gate = BookIntegrityGate(
            self.config.max_book_age_ns,
            cancel_callback or (lambda _reason, _now_ns: 0),
            venue_control_callback=venue_control_callback,
        )
        self.symbols = {
            venue: self.config.instruments.get(venue, self.config.instrument_id).symbol
            for venue in self.settings.venues
        }
        self.recorder = RotatingLiveRecorder(
            output_dir,
            session_id,
            rotate_seconds=self.settings.rotate_seconds,
            rotate_bytes=self.settings.rotate_bytes,
            durable=self.settings.durable,
            clock_ns=clock_ns,
        )
        self.structured_logs = StructuredLogStore(
            self.recorder.session_dir / "structured_logs.jsonl",
            self.recorder.session_dir / "structured_logs.sqlite",
        )
        self.ohlc_resampler = OrderBookOhlcResampler(
            self.settings.ohlc_interval_seconds
        )
        self.ohlc_writer = OhlcCsvWriter(
            self.recorder.session_dir / "ohlc_1m.csv",
            durable=self.settings.durable,
        )
        self.ohlc_artifact: dict[str, Any] | None = None
        self.ltp_resampler = LtpOhlcResampler(self.settings.ohlc_interval_seconds)
        self.ltp_writer = LtpOhlcCsvWriter(
            self.recorder.session_dir / "ltp_ohlc_1m.csv",
            durable=self.settings.durable,
        )
        self.ltp_artifact: dict[str, Any] | None = None
        self.publisher = publisher
        if self.publisher is None and self.settings.udp_enabled:
            self.publisher = UdpMulticastPublisher(UdpPublisherSettings(
                group=self.settings.udp_group,
                port=self.settings.udp_port,
                interface=self.settings.udp_interface,
                ttl=self.settings.udp_ttl,
                depth=self.settings.udp_depth,
                max_datagram_bytes=self.settings.udp_max_datagram_bytes,
            ))

    def request_stop(self) -> None:
        self.stop_event.set()

    def _connector_log(
        self, event_type: str, level: StructuredLogLevel, message: str,
        *, venue: str = "", host: str = "", ts_ns: int | None = None,
        source_event_id: str = "", raw_ref: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.structured_logs.append(StructuredLogStore.connector_record(
            session_id=self.recorder.session_id,
            ts_ns=(self.clock_ns() if ts_ns is None else ts_ns),
            event_type=event_type, level=level, message=message,
            venue=venue, host=host, instrument_id=self.config.instrument_id,
            source_event_id=source_event_id, raw_ref=raw_ref, payload=payload,
        ))

    def _binance_urls(self) -> tuple[str, str]:
        if self.settings.binance_market == "linear":
            ws_base = self.settings.binance_ws_base or "wss://fstream.binance.com/ws"
            rest_base = self.settings.binance_rest_base or "https://fapi.binance.com/fapi/v1"
        else:
            ws_base = self.settings.binance_ws_base or "wss://stream.binance.com:9443/ws"
            rest_base = self.settings.binance_rest_base or "https://api.binance.com/api/v3"
        symbol = self.symbols["BINANCE"]
        return (
            f"{ws_base.rstrip('/')}/{symbol.lower()}@depth@100ms",
            f"{rest_base.rstrip('/')}/depth?symbol={symbol}&limit=1000",
        )

    def _binance_trade_url(self) -> str:
        depth_url, _ = self._binance_urls()
        return depth_url.rsplit("@depth@100ms", 1)[0] + "@trade"

    def _bybit_url(self) -> str:
        return self.settings.bybit_ws_url or (
            "wss://stream.bybit.com/v5/public/"
            + ("linear" if self.settings.bybit_market == "linear" else "spot")
        )

    @staticmethod
    def _host(url: str) -> str:
        return url.split("//", 1)[-1].split("/", 1)[0]

    async def run(self) -> dict[str, Any]:
        self.started_ts_ns = self.clock_ns()
        self._connector_log(
            "SESSION_STARTED", StructuredLogLevel.INFO,
            "live market-data session started", ts_ns=self.started_ts_ns,
            payload={"venues": list(self.settings.venues)},
        )
        tasks = [
            asyncio.create_task(self._venue_forever(venue), name=f"live-{venue.lower()}")
            for venue in self.settings.venues
        ]
        if self.settings.trade_streams_enabled:
            tasks.extend(
                asyncio.create_task(
                    self._trade_forever(venue), name=f"trades-{venue.lower()}"
                )
                for venue in self.settings.venues
            )
        tasks.append(asyncio.create_task(self._ohlc_clock(), name="ohlc-clock"))
        tasks.append(asyncio.create_task(
            self._monitoring_clock(), name="pipeline-monitor"
        ))
        timer: asyncio.Task[None] | None = None
        if self.settings.max_runtime_seconds is not None:
            timer = asyncio.create_task(self._stop_after(self.settings.max_runtime_seconds))
        try:
            await self.stop_event.wait()
        finally:
            self.stop_event.set()
            for task in tasks:
                task.cancel()
            try:
                # Bounded on purpose: a task's own cancellation can be slow
                # to complete under sustained reconnect/DNS-outage
                # conditions (observed: the websockets library's connection
                # close handshake stalling well past its own close_timeout
                # while a reconnect attempt is in flight). Shutdown must not
                # wait on that indefinitely; an unfinished task here is
                # abandoned with the process exit that follows shortly
                # after, not left running.
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True), timeout=15,
                )
            except TimeoutError:
                unfinished = [task.get_name() for task in tasks if not task.done()]
                LOGGER.error(
                    "session shutdown: %d task(s) did not finish cancelling "
                    "within 15s and were abandoned: %s",
                    len(unfinished), unfinished,
                )
                self._connector_log(
                    "SHUTDOWN_TASK_ABANDONED", StructuredLogLevel.ERROR,
                    "one or more tasks did not finish cancelling before shutdown",
                    payload={"unfinished_tasks": unfinished},
                )
            if timer is not None:
                timer.cancel()
                with suppress(asyncio.CancelledError):
                    await timer
            self.stopped_ts_ns = self.clock_ns()
            self._connector_log(
                "SESSION_STOPPED", StructuredLogLevel.INFO,
                "live market-data session stopped", ts_ns=self.stopped_ts_ns,
            )
            for bar in self.ohlc_resampler.flush_partial():
                self._emit_ohlc(bar)
            for bar in self.ltp_resampler.flush_partial():
                self._emit_ltp_ohlc(bar)
            self.ohlc_artifact = self.ohlc_writer.close()
            self.ltp_artifact = self.ltp_writer.close()
            if self.publisher is not None:
                self.publisher.close()
            self._write_monitoring(self.stopped_ts_ns or self.clock_ns(), final=True)
        return self.recorder.close(self._service_summary())

    async def _stop_after(self, seconds: float) -> None:
        """Stop the session at ``seconds`` past start.

        Polls a deadline in short increments (the same pattern the OHLC and
        monitoring clocks use, both of which have run reliably for hours in
        this process) rather than a single long ``asyncio.sleep``. A cycle
        that ran for hours instead of its configured 30 minutes was traced
        to exactly that single-shot timer never firing under sustained
        connection churn; short, repeated waits are the defensive fix, and
        the loop below also self-heals if one iteration is ever skipped.
        """
        deadline_ns = self.clock_ns() + int(seconds * 1_000_000_000)
        poll_seconds = min(30.0, max(1.0, seconds / 20))
        while not self.stop_event.is_set():
            remaining_s = (deadline_ns - self.clock_ns()) / 1_000_000_000
            if remaining_s <= 0:
                break
            try:
                await asyncio.wait_for(
                    self.stop_event.wait(), timeout=min(poll_seconds, remaining_s)
                )
            except TimeoutError:
                continue
        if not self.stop_event.is_set():
            self.request_stop()

    async def _ohlc_clock(self) -> None:
        while not self.stop_event.is_set():
            now_ns = self.clock_ns()
            boundary_ns = self.ohlc_resampler.next_boundary_ns(now_ns)
            delay_seconds = max(0.001, (boundary_ns - now_ns) / 1_000_000_000)
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=delay_seconds)
            except TimeoutError:
                for bar in self.ohlc_resampler.finalize_until(self.clock_ns()):
                    self._emit_ohlc(bar)
                for bar in self.ltp_resampler.finalize_until(self.clock_ns()):
                    self._emit_ltp_ohlc(bar)

    async def _monitoring_clock(self) -> None:
        while not self.stop_event.is_set():
            self._write_monitoring(self.clock_ns(), final=False)
            self._check_runtime_watchdog()
            try:
                await asyncio.wait_for(
                    self.stop_event.wait(),
                    timeout=self.settings.monitor_interval_seconds,
                )
            except TimeoutError:
                pass

    def _check_runtime_watchdog(self) -> None:
        """Independent backstop for ``_stop_after``.

        Runs on the monitoring clock's own timer (a separate task, polling
        every ``monitor_interval_seconds``) so a failure specific to the
        shutdown timer's task does not leave the session unbounded.
        """
        if (
            self.settings.max_runtime_seconds is None
            or self.started_ts_ns is None
            or self.stop_event.is_set()
        ):
            return
        elapsed_s = (self.clock_ns() - self.started_ts_ns) / 1_000_000_000
        overrun_s = elapsed_s - self.settings.max_runtime_seconds
        if overrun_s > 5.0:
            self._connector_log(
                "RUNTIME_WATCHDOG_FORCED_STOP", StructuredLogLevel.ERROR,
                "max_runtime_seconds elapsed without the shutdown timer firing; "
                "the monitoring clock's watchdog is forcing the stop",
                payload={
                    "max_runtime_seconds": self.settings.max_runtime_seconds,
                    "elapsed_seconds": elapsed_s, "overrun_seconds": overrun_s,
                },
            )
            LOGGER.error(
                "runtime watchdog forced stop after %.0fs (configured %.0fs)",
                elapsed_s, self.settings.max_runtime_seconds,
            )
            self.request_stop()

    def _write_monitoring(self, now_ns: int, *, final: bool) -> dict[str, Any]:
        reconnects = sum(
            row.reconnects + row.trade_reconnects for row in self.stats.values()
        )
        stale_disconnects = sum(
            row.stale_disconnects + row.trade_stale_disconnects
            for row in self.stats.values()
        )
        total_parts = max(1, self.recorder.part_number)
        report = PipelineMonitor(self.config.monitoring_thresholds).build(
            mode="LIVE", generated_ts_ns=now_ns,
            correlation_id=self.recorder.session_id,
            job={
                "state": "COMPLETE" if final else "RUNNING",
                "mode": "CONTINUOUS",
            },
            data={
                "event_count": self.events_seen,
                "wire_records": self.recorder.wire_count,
                "canonical_events": self.recorder.canonical_count,
                "trade_ticks": self.recorder.trade_count,
                "latest_persisted_receive_ts_ns": (
                    self.recorder.last_persisted_receive_ts_ns
                ),
                "clock": "LOCAL_RECEIVE_TIME",
            },
            partitions={
                "total": total_parts,
                "complete": total_parts if final else len(self.recorder.parts),
                "active": 0 if final else int(self.recorder.part_number > len(self.recorder.parts)),
                "completeness": 1.0,
                "rejected": self.recorder.rejected_count,
            },
            books={
                venue: {
                    "instrument_id": book.instrument_id,
                    "state": book.state.value,
                    "age_ns": max(0, now_ns - (book.last_receive_ts_ns or now_ns)),
                    "sequence": book.sequence,
                    "gaps": book.stats.gaps,
                    "duplicates": book.stats.duplicates,
                    "out_of_order": book.stats.out_of_order,
                    "checksum_failures": book.stats.checksum_failures,
                }
                for venue, book in self.books.items()
            },
            versions={
                "canonical_schema_expected": "1.2.0",
                "canonical_schema_observed": ["1.2.0"],
                "wire_schema_version": WIRE_SCHEMA_VERSION,
                "schema_mismatches": 0,
                "instrument_master": self.config.instruments.version_snapshot(),
            },
            performance={"replay_events_per_second": 0},
            live={
                "reconnects": reconnects,
                "stale_disconnects": stale_disconnects,
                "persistence_lag_ns": self.recorder.max_persistence_lag_ns,
                "durable_fsync": self.settings.durable,
            },
            structured_logs=self.structured_logs.stats(),
        )
        PipelineMonitor.write(
            report,
            self.recorder.session_dir / "pipeline_monitoring.json",
            self.recorder.session_dir / "pipeline_metrics.prom",
        )
        return report

    async def _venue_forever(self, venue: str) -> None:
        stats = self.stats[venue]
        backoff = self.settings.reconnect_min_seconds
        while not self.stop_event.is_set():
            connections_before = stats.connections
            stats.connection_attempts += 1
            self._connector_log(
                "CONNECTION_ATTEMPT", StructuredLogLevel.INFO,
                "order-book connector attempt", venue=venue,
                payload={"attempt": stats.connection_attempts},
            )
            try:
                if venue == "BINANCE":
                    await self._run_binance_connection()
                else:
                    await self._run_bybit_connection()
                if not self.stop_event.is_set():
                    raise ConnectionError("public feed connection closed")
            except asyncio.CancelledError:
                raise
            except ResyncRequired as exc:
                stats.resyncs += 1
                stats.last_error = str(exc)
                LOGGER.warning("%s resynchronization required: %s", venue, exc)
                self._connector_log(
                    "RESYNC_REQUIRED", StructuredLogLevel.WARNING,
                    str(exc), venue=venue,
                )
            except Exception as exc:
                stats.last_error = f"{type(exc).__name__}: {exc}"
                LOGGER.warning("%s feed disconnected: %s", venue, stats.last_error)
                self._connector_log(
                    "CONNECTOR_ERROR", StructuredLogLevel.ERROR,
                    stats.last_error, venue=venue,
                )
            if not self.stop_event.is_set():
                self._invalidate_connection(venue)
            stats.disconnects += 1
            if self.stop_event.is_set():
                break
            stats.reconnects += 1
            self._connector_log(
                "RECONNECT_SCHEDULED", StructuredLogLevel.WARNING,
                "order-book reconnect scheduled", venue=venue,
                payload={"backoff_seconds": backoff},
            )
            if stats.connections > connections_before:
                backoff = self.settings.reconnect_min_seconds
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=backoff)
            except TimeoutError:
                pass
            backoff = min(self.settings.reconnect_max_seconds, backoff * 2)

    async def _trade_forever(self, venue: str) -> None:
        stats = self.stats[venue]
        backoff = self.settings.reconnect_min_seconds
        while not self.stop_event.is_set():
            connections_before = stats.trade_connections
            stats.trade_connection_attempts += 1
            self._connector_log(
                "TRADE_CONNECTION_ATTEMPT", StructuredLogLevel.INFO,
                "trade connector attempt", venue=venue,
                payload={"attempt": stats.trade_connection_attempts},
            )
            try:
                if venue == "BINANCE":
                    await self._run_binance_trade_connection()
                else:
                    await self._run_bybit_trade_connection()
                if not self.stop_event.is_set():
                    raise ConnectionError("public trade feed connection closed")
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                stats.trade_stale_disconnects += 1
                stats.last_error = "public trade feed stale"
                self._connector_log(
                    "TRADE_FEED_STALE", StructuredLogLevel.WARNING,
                    stats.last_error, venue=venue,
                )
            except Exception as exc:
                stats.last_error = f"{type(exc).__name__}: {exc}"
                LOGGER.warning("%s trade feed disconnected: %s", venue, stats.last_error)
                self._connector_log(
                    "TRADE_CONNECTOR_ERROR", StructuredLogLevel.ERROR,
                    stats.last_error, venue=venue,
                )
            stats.trade_disconnects += 1
            if self.stop_event.is_set():
                break
            stats.trade_reconnects += 1
            if stats.trade_connections > connections_before:
                backoff = self.settings.reconnect_min_seconds
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=backoff)
            except TimeoutError:
                pass
            backoff = min(self.settings.reconnect_max_seconds, backoff * 2)

    async def _run_binance_trade_connection(self) -> None:
        venue = "BINANCE"
        url = self._binance_trade_url()
        async with self.websocket_connector(venue, url) as websocket:
            self.stats[venue].trade_connections += 1
            LOGGER.info("BINANCE trades connected: %s", self._host(url))
            self._connector_log(
                "TRADE_CONNECTED", StructuredLogLevel.INFO,
                "trade connector established", venue=venue, host=self._host(url),
            )
            while not self.stop_event.is_set():
                frame = await asyncio.wait_for(
                    websocket.recv(), timeout=self.settings.trade_stale_seconds
                )
                self._process_trade_frame(venue, url, frame, channel="trade")

    async def _run_bybit_trade_connection(self) -> None:
        venue = "BYBIT"
        url = self._bybit_url()
        topic = f"publicTrade.{self.symbols[venue]}"
        async with self.websocket_connector(venue, url) as websocket:
            self.stats[venue].trade_connections += 1
            LOGGER.info("BYBIT trades connected: %s", self._host(url))
            self._connector_log(
                "TRADE_CONNECTED", StructuredLogLevel.INFO,
                "trade connector established", venue=venue, host=self._host(url),
            )
            await websocket.send(json.dumps({"op": "subscribe", "args": [topic]}))
            while not self.stop_event.is_set():
                frame = await asyncio.wait_for(
                    websocket.recv(), timeout=self.settings.trade_stale_seconds
                )
                self._process_trade_frame(venue, url, frame, channel=topic)

    async def _run_binance_connection(self) -> None:
        venue = "BINANCE"
        ws_url, snapshot_url = self._binance_urls()
        stats = self.stats[venue]
        async with self.websocket_connector(venue, ws_url) as websocket:
            stats.connections += 1
            self._begin_connection(venue)
            LOGGER.info("BINANCE connected: %s", self._host(ws_url))
            self._connector_log(
                "CONNECTED", StructuredLogLevel.INFO,
                "order-book connector established", venue=venue,
                host=self._host(ws_url),
            )
            snapshot_task = asyncio.create_task(
                self.http_getter(snapshot_url, self.settings.http_timeout_seconds)
            )
            try:
                await self._drain_binance_until_snapshot(
                    websocket, snapshot_task, ws_url, snapshot_url
                )
                while not self.stop_event.is_set():
                    frame = await self._recv_fresh(venue, websocket)
                    state = self._process_websocket_frame(venue, ws_url, frame)
                    if state in (
                        BookState.GAP_DETECTED, BookState.CHECKSUM_FAILED,
                        BookState.INVALID_DATA,
                    ):
                        raise ResyncRequired(self.books[venue].invalid_reason)
            finally:
                if not snapshot_task.done():
                    snapshot_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await snapshot_task

    async def _drain_binance_until_snapshot(
        self,
        websocket: WebSocketLike,
        snapshot_task: asyncio.Task[tuple[str | bytes, int]],
        websocket_url: str,
        snapshot_url: str,
    ) -> None:
        venue = "BINANCE"
        while not snapshot_task.done():
            recv_task = asyncio.create_task(websocket.recv())
            timeout = self._remaining_fresh_seconds(venue)
            done, _ = await asyncio.wait(
                {recv_task, snapshot_task},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                recv_task.cancel()
                with suppress(asyncio.CancelledError):
                    await recv_task
                self._mark_stale(venue)
                raise ResyncRequired("market_data_stale_while_fetching_snapshot")
            if recv_task in done:
                frame = recv_task.result()
                self._process_websocket_frame(
                    venue, websocket_url, frame, channel="depth@100ms"
                )
            else:
                recv_task.cancel()
                with suppress(asyncio.CancelledError):
                    await recv_task
        original, receive_ts_ns = snapshot_task.result()
        state = self._process_binance_snapshot(snapshot_url, original, receive_ts_ns)
        if state != BookState.VALID:
            raise ResyncRequired(self.books[venue].invalid_reason)

    async def _run_bybit_connection(self) -> None:
        venue = "BYBIT"
        url = self._bybit_url()
        stats = self.stats[venue]
        topic = f"orderbook.{self.settings.bybit_depth}.{self.symbols[venue]}"
        async with self.websocket_connector(venue, url) as websocket:
            stats.connections += 1
            self._begin_connection(venue)
            LOGGER.info("BYBIT connected: %s", self._host(url))
            self._connector_log(
                "CONNECTED", StructuredLogLevel.INFO,
                "order-book connector established", venue=venue,
                host=self._host(url),
            )
            await websocket.send(json.dumps({"op": "subscribe", "args": [topic]}))
            while not self.stop_event.is_set():
                frame = await self._recv_fresh(venue, websocket)
                state = self._process_websocket_frame(venue, url, frame, channel=topic)
                if state in (
                        BookState.GAP_DETECTED, BookState.CHECKSUM_FAILED,
                        BookState.INVALID_DATA,
                    ):
                    raise ResyncRequired(self.books[venue].invalid_reason)

    def _remaining_fresh_seconds(self, venue: str) -> float:
        book = self.books[venue]
        if book.state != BookState.VALID or book.last_receive_ts_ns is None:
            return self.settings.initial_sync_timeout_seconds
        remaining_ns = self.config.max_book_age_ns - (
            self.clock_ns() - book.last_receive_ts_ns
        )
        return max(0.001, remaining_ns / 1_000_000_000)

    async def _recv_fresh(
        self, venue: str, websocket: WebSocketLike
    ) -> str | bytes:
        try:
            frame = await asyncio.wait_for(
                websocket.recv(), timeout=self._remaining_fresh_seconds(venue)
            )
        except TimeoutError as exc:
            self._mark_stale(venue)
            raise ResyncRequired("market_data_stale") from exc
        return frame

    def _mark_stale(self, venue: str) -> None:
        book = self.books[venue]
        book.mark_stale(self.clock_ns(), self.config.max_book_age_ns)
        self.stats[venue].stale_disconnects += 1
        self.book_gate.allow(tuple(self.books.values()), self.clock_ns())
        self._publish_book(book, None)

    def _begin_connection(self, venue: str) -> None:
        self.books[venue].start_resync("connection_snapshot_required")
        self.book_gate.allow(tuple(self.books.values()), self.clock_ns())
        self._publish_book(self.books[venue], None)

    def _invalidate_connection(self, venue: str) -> None:
        self.books[venue].start_resync("connection_lost")
        self.book_gate.allow(tuple(self.books.values()), self.clock_ns())
        self._publish_book(self.books[venue], None)

    def _capture_and_parse(
        self,
        venue: str,
        source_url: str,
        channel: str,
        transport: str,
        original_frame: str | bytes,
        receive_ts_ns: int,
        adapter_version: str,
        *,
        trade_stream: bool = False,
    ) -> tuple[WireFrame, RawFrame, dict[str, Any]] | None:
        stats = self.stats[venue]
        wire = self.recorder.capture_wire(
            venue=venue,
            channel=channel,
            host=self._host(source_url),
            transport=transport,
            receive_ts_ns=receive_ts_ns,
            original_frame=original_frame,
        )
        if trade_stream:
            stats.trade_raw_frames += 1
        else:
            stats.raw_frames += 1
        try:
            if isinstance(original_frame, bytes):
                payload = json.loads(original_frame.decode("utf-8"))
            else:
                payload = json.loads(original_frame)
            if not isinstance(payload, dict):
                raise ValueError("exchange frame must decode to a JSON object")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            if trade_stream:
                stats.trade_parse_errors += 1
            else:
                stats.parse_errors += 1
            self.recorder.record_rejection(wire, exc, stage="PARSE")
            self._connector_log(
                "FRAME_PARSE_REJECTED", StructuredLogLevel.ERROR,
                str(exc), venue=venue, host=self._host(source_url),
                ts_ns=receive_ts_ns, payload={"wire_ref": wire.wire_ref, "channel": channel},
            )
            return None
        raw = self.recorder.append_payload(
            wire,
            payload=payload,
            vendor=venue,
            adapter_version=adapter_version,
        )
        return wire, raw, payload

    def _process_trade_frame(
        self,
        venue: str,
        url: str,
        original_frame: str | bytes,
        *,
        channel: str,
    ) -> int:
        receive_ts_ns = self.clock_ns()
        adapter_version = (
            "binance-trade-v1" if venue == "BINANCE" else "bybit-public-trade-v1"
        )
        captured = self._capture_and_parse(
            venue,
            url,
            channel,
            "WEBSOCKET",
            original_frame,
            receive_ts_ns,
            adapter_version,
            trade_stream=True,
        )
        if captured is None:
            return 0
        wire, raw, payload = captured
        is_trade = (
            payload.get("e") == "trade" and "p" in payload
            if venue == "BINANCE"
            else str(payload.get("topic", "")).startswith("publicTrade.")
            and isinstance(payload.get("data"), list)
        )
        if not is_trade:
            self.stats[venue].control_frames += 1
            self.recorder.record_control(wire, raw)
            return 0
        try:
            if venue == "BINANCE":
                ticks = (normalize_binance_trade(
                    payload, receive_ts_ns, self.config.instrument_id, raw.raw_ref
                ),)
            else:
                ticks = normalize_bybit_trades(
                    payload, receive_ts_ns, self.config.instrument_id, raw.raw_ref
                )
            for tick in ticks:
                self._resolve(venue, tick.symbol)
        except (KeyError, TypeError, ValueError) as exc:
            self.stats[venue].trade_normalization_errors += 1
            self.recorder.record_rejection(
                wire, exc, raw_ref=raw.raw_ref, stage="TRADE_NORMALIZE"
            )
            return 0
        valid_ticks = []
        for tick in ticks:
            reason = self._trade_rejection(venue, tick)
            if reason:
                self.stats[venue].invalid_trades += 1
                self.recorder.record_rejection(
                    wire, ValueError(reason), raw_ref=raw.raw_ref, stage="TRADE_VALIDATE"
                )
                continue
            valid_ticks.append(tick)
        ticks = tuple(valid_ticks)
        for tick in ticks:
            if self.trade_listener is not None:
                self.trade_listener(tick)
            self.recorder.record_trade(wire, raw, tick)
            self.stats[venue].trade_ticks += 1
            if self.publisher is not None:
                self.publisher.publish_trade(tick, self.clock_ns())
            for bar in self.ltp_resampler.update(tick):
                self._emit_ltp_ohlc(bar)
        return len(ticks)

    def _trade_rejection(self, venue: str, tick: TradeTick) -> str:
        """Reject malformed prints and prints far from a valid book (MR-02)."""
        if not (tick.price.is_finite() and tick.quantity.is_finite()):
            return "non_finite_trade"
        if tick.price <= 0:
            return "non_positive_trade_price"
        if tick.quantity <= 0:
            return "non_positive_trade_quantity"
        if tick.aggressor_side not in {"BUY", "SELL"}:
            return "unknown_aggressor_side"
        book = self.books.get(venue)
        if book is not None and book.state == BookState.VALID:
            mid = (book.best_bid + book.best_ask) / Decimal("2")
            deviation = abs(tick.price - mid) / mid * Decimal("10000")
            if deviation > Decimal(str(self.settings.max_trade_deviation_bps)):
                return "trade_outside_book_band"
        return ""

    def _process_websocket_frame(
        self,
        venue: str,
        url: str,
        original_frame: str | bytes,
        *,
        channel: str | None = None,
    ) -> BookState | None:
        receive_ts_ns = self.clock_ns()
        adapter_version = (
            "binance-depth-v1" if venue == "BINANCE" else "bybit-depth-v1"
        )
        captured = self._capture_and_parse(
            venue,
            url,
            channel or "depth@100ms",
            "WEBSOCKET",
            original_frame,
            receive_ts_ns,
            adapter_version,
        )
        if captured is None:
            return None
        wire, raw, payload = captured
        if not self._is_book_payload(venue, payload):
            self.stats[venue].control_frames += 1
            self.recorder.record_control(wire, raw)
            return None
        try:
            event = self._normalize(venue, payload, receive_ts_ns, raw.raw_ref)
        except (KeyError, TypeError, ValueError) as exc:
            self.stats[venue].normalization_errors += 1
            self.recorder.record_rejection(
                wire, exc, raw_ref=raw.raw_ref, stage="NORMALIZE"
            )
            return None
        return self._apply_and_record(wire, raw, event)

    def _process_binance_snapshot(
        self, url: str, original_frame: str | bytes, receive_ts_ns: int
    ) -> BookState | None:
        venue = "BINANCE"
        captured = self._capture_and_parse(
            venue,
            url,
            "rest-depth-snapshot",
            "HTTPS",
            original_frame,
            receive_ts_ns,
            "binance-rest-depth-v1",
        )
        if captured is None:
            return None
        wire, raw, payload = captured
        try:
            event = normalize_binance_depth(
                payload,
                receive_ts_ns,
                self._resolve,
                raw_ref=raw.raw_ref,
                adapter_version="binance-rest-depth-v1",
                symbol_override=self.symbols[venue],
            )
        except (KeyError, TypeError, ValueError) as exc:
            self.stats[venue].normalization_errors += 1
            self.recorder.record_rejection(
                wire, exc, raw_ref=raw.raw_ref, stage="NORMALIZE"
            )
            return None
        return self._apply_and_record(wire, raw, event)

    @staticmethod
    def _is_book_payload(venue: str, payload: dict[str, Any]) -> bool:
        if venue == "BINANCE":
            return "u" in payload and ("b" in payload or "a" in payload)
        return (
            str(payload.get("topic", "")).startswith("orderbook.")
            and payload.get("type") in ("snapshot", "delta")
            and isinstance(payload.get("data"), dict)
        )

    def _resolve(self, venue: str, symbol: str) -> str:
        expected = self.symbols[venue]
        if symbol != expected:
            raise ValueError(f"unexpected symbol {venue}/{symbol}; expected {expected}")
        return self.config.instrument_id

    def _normalize(
        self, venue: str, payload: dict[str, Any], receive_ts_ns: int, raw_ref: str
    ) -> MarketEvent:
        if venue == "BINANCE":
            return normalize_binance_depth(
                payload, receive_ts_ns, self._resolve, raw_ref=raw_ref
            )
        return normalize_bybit_depth(
            payload, receive_ts_ns, self._resolve, raw_ref=raw_ref
        )

    def _apply_and_record(
        self, wire: WireFrame, raw: RawFrame, event: MarketEvent
    ) -> BookState:
        self.recorder.record_event(wire, raw, event)
        if self.pre_event_listener is not None:
            # Timers (in-flight arrivals) run against the book *before* this update.
            self.pre_event_listener(event)
        book = self.books[event.venue]
        previous_event_id = book.last_event_id
        previous_state = book.state
        pending_events = {
            pending.event_id: pending for pending in book.pending_deltas.values()
        }
        state = book.apply(event)
        accepted = book.last_event_id is not None and book.last_event_id != previous_event_id
        source_event = (
            event
            if book.last_event_id == event.event_id
            else pending_events.get(book.last_event_id or "", event)
        )
        self.book_gate.allow(tuple(self.books.values()), event.receive_ts_ns)
        if accepted or state != previous_state:
            self._publish_book(book, source_event)
        if accepted:
            for bar in self.ohlc_resampler.update(
                book, source_event, availability_ts_ns=event.receive_ts_ns
            ):
                self._emit_ohlc(bar)
        self.stats[event.venue].canonical_events += 1
        if accepted or state != previous_state:
            self._connector_log(
                "BOOK_STATE", (
                    StructuredLogLevel.INFO if state == BookState.VALID
                    else StructuredLogLevel.WARNING
                ),
                f"book state {state.value}", venue=event.venue,
                host=wire.host, ts_ns=event.receive_ts_ns,
                source_event_id=event.event_id, raw_ref=event.raw_ref,
                payload={"state": state.value, "sequence": book.sequence},
            )
        if self.event_listener is not None:
            self.event_listener(event, book, state, accepted)
        self.events_seen += 1
        if (
            self.settings.max_events is not None
            and self.events_seen >= self.settings.max_events
        ):
            self.request_stop()
        return state

    def _publish_book(self, book: OrderBook, event: MarketEvent | None) -> None:
        if self.publisher is not None:
            self.publisher.publish_orderbook(book, event, self.clock_ns())

    def _emit_ohlc(self, bar: OhlcBar) -> None:
        self.ohlc_writer.append(bar)
        if self.publisher is not None:
            self.publisher.publish_ohlc(bar, self.clock_ns())

    def _emit_ltp_ohlc(self, bar: LtpOhlcBar) -> None:
        self.ltp_writer.append(bar)
        if self.publisher is not None:
            self.publisher.publish_ltp_ohlc(bar, self.clock_ns())

    def _service_summary(self) -> dict[str, Any]:
        return {
            "mode": "PUBLIC_MARKET_DATA_ONLY",
            "live_order_routing": False,
            "started_ts_ns": self.started_ts_ns,
            "stopped_ts_ns": self.stopped_ts_ns,
            "duration_ns": (
                (self.stopped_ts_ns or 0) - (self.started_ts_ns or 0)
            ),
            "instrument_id": self.config.instrument_id,
            "symbols": self.symbols,
            "max_book_age_ns": self.config.max_book_age_ns,
            "events_seen": self.events_seen,
            "pipeline_monitoring": {
                "report": "pipeline_monitoring.json",
                "prometheus_metrics": "pipeline_metrics.prom",
                "refresh_interval_seconds": self.settings.monitor_interval_seconds,
                "max_persistence_lag_ns": self.recorder.max_persistence_lag_ns,
            },
            "structured_logs": self.structured_logs.stats(),
            "ohlc": {
                **self.ohlc_resampler.stats_snapshot(),
                "artifact": self.ohlc_artifact,
            },
            "ltp_ohlc": {
                **self.ltp_resampler.stats_snapshot(),
                "artifact": self.ltp_artifact,
                "clock": "LOCAL_RECEIVE_TIME",
                "price_source": "PUBLIC_TRADE_LTP",
            },
            "udp": (
                self.publisher.stats_snapshot()
                if self.publisher is not None
                else {"enabled": False}
            ),
            "venues": {venue: asdict(stats) for venue, stats in self.stats.items()},
            "strategy_gate": {
                "suspended": self.book_gate.suspended,
                "reason": self.book_gate.reason,
                "suspend_count": self.book_gate.suspend_count,
                "resume_count": self.book_gate.resume_count,
                "cancel_requests": self.book_gate.cancel_requests,
                "cancelled_orders": self.book_gate.cancelled_orders,
                "venue_transitions": self.book_gate.venue_transitions,
                "venues": {
                    venue: self.book_gate.venue_state(venue)
                    for venue in self.settings.venues
                },
            },
            "books": {
                venue: {
                    "state": book.state.value,
                    "invalid_reason": book.invalid_reason,
                    "sequence": book.sequence,
                    "last_event_id": book.last_event_id,
                    "last_receive_ts_ns": book.last_receive_ts_ns,
                    "snapshot_sha256": book.snapshot_hash(),
                    "stats": asdict(book.stats),
                }
                for venue, book in self.books.items()
            },
        }


def default_live_session_id() -> str:
    return datetime.now(timezone.utc).strftime("live-%Y%m%dT%H%M%SZ")


def verify_wire_file(path: Path) -> dict[str, Any]:
    import base64

    previous = GENESIS_HASH
    count = 0
    first_ref = None
    last_ref = None
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            record = json.loads(line)
            recorded_hash = record.pop("record_hash")
            if record.get("previous_record_hash") != previous:
                raise ValueError(f"wire hash-chain break at line {line_number}")
            if record.get("frame_encoding") == "BASE64":
                frame_bytes = base64.b64decode(record.get("frame_data", ""), validate=True)
            else:
                frame_bytes = record.get("frame_data", "").encode("utf-8")
            if sha256(frame_bytes).hexdigest() != record.get("frame_sha256"):
                raise ValueError(f"wire frame checksum mismatch at line {line_number}")
            calculated = sha256(
                RotatingLiveRecorder._canonical_json(record).encode()
            ).hexdigest()
            if calculated != recorded_hash:
                raise ValueError(f"wire record checksum mismatch at line {line_number}")
            previous = recorded_hash
            count += 1
            first_ref = first_ref or record["wire_ref"]
            last_ref = record["wire_ref"]
    return {
        "valid": True,
        "record_count": count,
        "first_wire_ref": first_ref,
        "last_wire_ref": last_ref,
        "terminal_record_hash": previous,
        "wire_file_sha256": RotatingLiveRecorder._file_sha256(path),
    }
