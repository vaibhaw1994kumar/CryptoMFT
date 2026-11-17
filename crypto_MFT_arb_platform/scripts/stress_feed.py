"""A9 stress proof (owner 16 Nov, Order 3): a synthetic burst of book events through the LIVE path on wall time.

What it does: builds a `LivePaperSession` (the live service, its book gate, the shared `TradingSession`) on the perpetual research config, replaces only the network (a fake websocket connector whose frames are handed
over on a wall-clock schedule, a fake REST snapshot), and drives `--rate` book events per second on each venue for `--seconds`. The frames go through the real parse (`_process_websocket_frame`: capture, JSON, the
venue normaliser), the real book (sequencing, gap and staleness rules, the book gate) and the real session path (`_on_event` -> `TradingSession.on_event`, with the default strategy evaluating on every event).
Time is the HOST WALL CLOCK (`time.time_ns`, `time.perf_counter_ns`), never a replay clock. Nothing is simulated except the venue.

What it reports (aggregates only, JSON): frames sent and processed per venue; processing time per event (frame handed to the service until the session returned) p50 / p99 / max; harness lag (how far behind its
own schedule the frames were handed over: a service that cannot keep up shows here); book age p50 / p99 / max as the staleness gate measures it (sampled every 10 ms); stale-book transitions, gaps, out-of-order and
duplicate deltas, normalisation errors, book gate suspensions; dropped frames (sent minus processed); CPU seconds over the wall window (user + system, the share of one core); the machine. It does not tune anything:
if the run fails, `where_the_time_goes` (a cProfile of one second at the same rate, top 12 cumulative entries) says where.

Run (from crypto_MFT_arb_platform/):   PYTHONPATH=src python3.12 scripts/stress_feed.py --rate 10000 --seconds 5 --out ../out/integration/stress/stress_feed_report.json
EXPLORATORY: this Mac, one process, a synthetic top-of-book stream (a bid and an ask level updated per frame around a fixed mid), no strategy tuning. A pass here says the parse, book and session path keeps up on this
machine with this stream; it is not a statement about the venues' real traffic, whose per-message size and burst shape differ."""
from __future__ import annotations

import argparse
import asyncio
import cProfile
import io
import json
import platform
import pstats
import resource
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from crypto_stat_arb.live_feed import LiveFeedSettings
from crypto_stat_arb.paper_live import LivePaperSession

CONFIG = Path(__file__).resolve().parents[1] / "config" / "perp_research.json"
MID = 100.0
ARRIVAL_STAMP = True                       # --arrival-stamp on|off (A9 fix b); off reproduces the old blind spot


def percentile(values: list[int], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * p))] / 1e6      # ms


def binance_frames(symbol: str, n: int, base_u: int) -> list[str]:
    out = []
    for i in range(n):
        u = base_u + i
        k = 1 + (i % 20)
        side = (i % 2 == 0)
        px = f"{MID - 0.01 * k:.2f}" if side else f"{MID + 0.01 * k:.2f}"
        qty = f"{1 + (i * 7) % 50}"
        ms = 1_700_000_000_000 + i // 10
        out.append(json.dumps({"e": "depthUpdate", "E": ms, "T": ms, "s": symbol, "U": u, "u": u, "pu": u - 1, "b": [[px, qty]] if side else [], "a": [] if side else [[px, qty]]}))
    return out


def bybit_frames(symbol: str, n: int, depth: int) -> list[str]:
    ms = 1_700_000_000_000
    snap = json.dumps({"topic": f"orderbook.{depth}.{symbol}", "type": "snapshot", "ts": ms, "data": {"s": symbol, "u": 1, "seq": 1, "b": [[f"{MID - 0.01 * k:.2f}", "10"] for k in range(1, 21)], "a": [[f"{MID + 0.01 * k:.2f}", "10"] for k in range(1, 21)]}})
    out = [snap]
    for i in range(n - 1):
        u = 2 + i
        k = 1 + (i % 20)
        side = (i % 2 == 0)
        px = f"{MID - 0.01 * k:.2f}" if side else f"{MID + 0.01 * k:.2f}"
        qty = f"{1 + (i * 7) % 50}"
        out.append(json.dumps({"topic": f"orderbook.{depth}.{symbol}", "type": "delta", "ts": ms + i // 10, "data": {"s": symbol, "u": u, "seq": u, "b": [[px, qty]] if side else [], "a": [] if side else [[px, qty]]}}))
    return out


class PacedSocket:
    """The n-th frame is handed over at t0 + n / rate on the wall clock (never earlier); when the consumer is behind, frames come back to back and the lag is recorded."""

    def __init__(self, frames: list[str], rate: float, clock_t0: list, handed: list, start_index: int = 0, prefix: int = 0):
        self.frames, self.rate, self.t0, self.handed = frames, rate, clock_t0, handed
        self.start_index, self.prefix = start_index, prefix            # a reconnect resumes at the schedule's current position, with `prefix` snapshot frames delivered at once
        self.i = 0
        self.sched: list[int] = []
        self.lag: list[int] = []
        self._never = asyncio.Event()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def send(self, message):
        return None

    async def recv(self):
        if self.i >= len(self.frames):
            await self._never.wait()
        if self.t0[0] is None:
            self.t0[0] = time.perf_counter_ns()
        now = time.perf_counter_ns()
        target = now if self.i < self.prefix else self.t0[0] + int((self.start_index + self.i - self.prefix) * 1e9 / self.rate)
        if target > now:
            await asyncio.sleep((target - now) / 1e9)                 # the frame has not arrived yet
        # else: it has arrived and sat in the connection's buffer: it is handed over at once, without a pause, as a buffered websocket does
        handed_ts = time.perf_counter_ns()
        self.lag.append(max(0, handed_ts - target))
        frame = self.frames[self.i]
        self.i += 1
        self.handed.append(handed_ts)
        return frame


class BybitSocket:
    """Bybit serves the book and the ticker streams on one URL: the connection is the book's only if it subscribes to an orderbook topic. `make_book(k0)` builds the paced socket then, at the schedule's current position."""

    def __init__(self, make_book):
        self.make_book, self.book, self._never = make_book, None, asyncio.Event()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def send(self, message):
        if self.book is None and "orderbook" in message:
            self.book = self.make_book()

    async def recv(self):
        if self.book is None:
            await self._never.wait()
        return await self.book.recv()


class IdleSocket:
    def __init__(self):
        self._never = asyncio.Event()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def send(self, message):
        return None

    async def recv(self):
        await self._never.wait()


async def one_run(rate: float, seconds: float, profile_seconds: float | None = None) -> dict:
    n = int(rate * seconds)
    with tempfile.TemporaryDirectory() as directory:
        settings = LiveFeedSettings(udp_enabled=False, durable=False, binance_market="linear", bybit_market="linear", trade_streams_enabled=False, reconnect_min_seconds=0.05, reconnect_max_seconds=0.1,
                                    monitor_interval_seconds=1.0, arrival_stamp=ARRIVAL_STAMP)
        t0, handed = [None], {"BINANCE": [], "BYBIT": []}
        sockets: dict[str, PacedSocket] = {}
        paper_holder: dict = {}

        async def http_getter(url, timeout):
            await asyncio.sleep(0.01)
            return json.dumps({"lastUpdateId": resume["binance_k0"] + 1, "bids": [[f"{MID - 0.01 * k:.2f}", "10"] for k in range(1, 21)], "asks": [[f"{MID + 0.01 * k:.2f}", "10"] for k in range(1, 21)]}), time.time_ns()

        all_frames: dict[str, list[str]] = {}
        connections = {"BINANCE": 0, "BYBIT": 0}
        resume = {"binance_k0": 0}
        socket_history: dict[str, list] = {"BINANCE": [], "BYBIT": []}

        def build_book_socket(venue):
            symbol = paper_holder["paper"].service.symbols[venue]
            if venue not in all_frames:
                all_frames[venue] = binance_frames(symbol, n, 2) if venue == "BINANCE" else bybit_frames(symbol, n, settings.bybit_depth)
            connections[venue] += 1
            k0 = min(n - 1, max(0, int((time.perf_counter_ns() - t0[0]) * rate / 1e9))) if t0[0] is not None and connections[venue] > 1 else 0
            if venue == "BINANCE":
                resume["binance_k0"] = k0
                sock = PacedSocket(all_frames[venue][k0:], rate, t0, handed[venue], start_index=k0)
            elif k0 == 0:
                sock = PacedSocket(all_frames[venue], rate, t0, handed[venue])
            else:                                                 # a reconnect: a fresh snapshot (the static levels, u = k0) first, then the deltas from the schedule's current position
                snap = json.loads(all_frames[venue][0])
                snap["data"]["u"] = k0
                sock = PacedSocket([json.dumps(snap)] + all_frames[venue][k0:], rate, t0, handed[venue], start_index=k0, prefix=1)
            sockets[venue] = sock
            socket_history[venue].append(sock)
            return sock

        def connector(venue, url):
            if venue == "BINANCE" and "depth" in url:
                return build_book_socket("BINANCE")
            if venue == "BYBIT":
                return BybitSocket(lambda: build_book_socket("BYBIT"))
            return IdleSocket()

        paper = LivePaperSession(CONFIG, Path(directory) / "live", "stress", settings, websocket_connector=connector, http_getter=http_getter, tick_seconds=0.05)
        paper_holder["paper"] = paper
        proc_ns: dict[str, list[int]] = {"BINANCE": [], "BYBIT": []}
        e2e_ns: dict[str, list[int]] = {"BINANCE": [], "BYBIT": []}      # ground truth: the frame's SCHEDULED arrival to the end of its processing, independent of the service's own stamps
        real_on_event = paper._on_event

        def timed_on_event(event, book, state, accepted):
            start = time.perf_counter_ns()
            real_on_event(event, book, state, accepted)
            end = time.perf_counter_ns()
            proc_ns[event.venue].append(end - start)
            index = event.sequence - (2 if event.venue == "BINANCE" else 1)       # the frame's index in the schedule (the REST snapshot and the Bybit snapshot sit at the start)
            if index >= 0 and t0[0] is not None:
                e2e_ns[event.venue].append(max(0, end - (t0[0] + int(index * 1e9 / rate))))
        paper._on_event = timed_on_event                              # the service holds the bound method it was given at build time: re-point it
        paper.service.event_listener = timed_on_event
        ages: dict[str, list[int]] = {"BINANCE": [], "BYBIT": []}
        stale_seen = {"BINANCE": 0, "BYBIT": 0}

        async def sampler():
            while True:
                await asyncio.sleep(0.01)
                if len(sockets) < 2 or all(sk.i >= len(sk.frames) for sk in sockets.values()):
                    continue                                          # only while the burst is running: a stream that has ended is not a stale book
                now = time.time_ns()
                for venue, book in paper.service.books.items():
                    if book.last_receive_ts_ns is not None:
                        ages[venue].append(max(0, now - book.last_receive_ts_ns))
                    if book.state.value == "STALE":
                        stale_seen[venue] += 1

        async def stopper():
            while any(s.i < len(s.frames) for s in sockets.values()) or len(sockets) < 2:
                await asyncio.sleep(0.05)
            deadline = time.perf_counter() + max(60.0, seconds * 10)          # a backlog is given time to drain: frames still unprocessed at the deadline are reported as dropped
            while time.perf_counter() < deadline and any(paper.service.stats[v].book_stream_frames + paper.service.arrival_stats.get(v, {}).get("backlog_frames_discarded", 0) < sum(x.i for x in socket_history[v]) for v in sockets):
                await asyncio.sleep(0.05)
            if profiler is not None:
                profiler.disable()                                    # the profile covers the burst and its drain, not the shutdown summary
            paper.service.request_stop()                              # at once: after the last frame the feed is silent, and a silent feed is (rightly) a stale one

        cpu0, wall0 = resource.getrusage(resource.RUSAGE_SELF), time.perf_counter()
        profiler = cProfile.Profile() if profile_seconds else None
        tasks = [asyncio.create_task(sampler()), asyncio.create_task(stopper())]
        if profiler is not None:
            profiler.enable()
        summary = await asyncio.wait_for(paper.run(), timeout=seconds * 10 + 120)
        wall = time.perf_counter() - wall0
        cpu1 = resource.getrusage(resource.RUSAGE_SELF)
        for t in tasks:
            t.cancel()
        books = paper.service.books
        report = {"rate_per_s_per_venue": rate, "seconds": seconds, "frames_scheduled_per_venue": n, "wall_s": round(wall, 3), "venues": {}}
        for venue in ("BINANCE", "BYBIT"):
            stats, book, sock = paper.service.stats[venue], books[venue], sockets.get(venue)
            handed_total = sum(x.i for x in socket_history[venue])
            lag_all = [v for x in socket_history[venue] for v in x.lag]
            discarded = paper.service.arrival_stats.get(venue, {}).get("backlog_frames_discarded", 0)
            report["venues"][venue] = {
                "connections": connections[venue], "frames_handed": handed_total, "book_stream_frames": stats.book_stream_frames, "canonical_events": stats.canonical_events,
                "backlog_frames_discarded": discarded, "dropped_frames": handed_total - stats.book_stream_frames - discarded, "normalization_errors": stats.normalization_errors,
                "processing_ms": {"n": len(proc_ns[venue]), "p50": percentile(proc_ns[venue], 0.5), "p99": percentile(proc_ns[venue], 0.99), "max": percentile(proc_ns[venue], 1.0)},
                "end_to_end_lag_ms": {"n": len(e2e_ns[venue]), "p50": percentile(e2e_ns[venue], 0.5), "p99": percentile(e2e_ns[venue], 0.99), "max": percentile(e2e_ns[venue], 1.0)},
                "arrival_stats": dict(paper.service.arrival_stats.get(venue, {})),
                "harness_lag_ms": {"p50": percentile(lag_all, 0.5), "p99": percentile(lag_all, 0.99), "max": percentile(lag_all, 1.0)},
                "book_age_ms": {"n": len(ages[venue]), "p50": percentile(ages[venue], 0.5), "p99": percentile(ages[venue], 0.99), "max": percentile(ages[venue], 1.0)},
                "stale_book_transitions": book.stats.stale_transitions, "stale_samples": stale_seen[venue], "gaps": book.stats.gaps, "out_of_order": book.stats.out_of_order, "duplicates": book.stats.duplicates,
                "resyncs": book.stats.resyncs, "book_state_at_end": book.state.value, "reconnects": getattr(stats, "book_reconnects", None),
            }
        report["book_gate_suspended_at_end"] = bool(paper.service.book_gate.suspended)
        report["session_events_processed"] = paper.session.events_processed
        cpu = (cpu1.ru_utime - cpu0.ru_utime) + (cpu1.ru_stime - cpu0.ru_stime)
        report["cpu"] = {"user_plus_system_s": round(cpu, 3), "share_of_one_core": round(cpu / wall, 3), "max_rss_mb": round(cpu1.ru_maxrss / (1024 * 1024 if sys.platform == "darwin" else 1024), 1)}
        if profiler is not None:
            buf = io.StringIO()
            stats = pstats.Stats(profiler, stream=buf)
            stats.sort_stats("tottime").print_stats(18)
            report["where_the_time_goes"] = {"by_own_time_top18": [l[:170] for l in buf.getvalue().splitlines() if l.strip()][:30]}
            buf2 = io.StringIO()
            pstats.Stats(profiler, stream=buf2).sort_stats("cumulative").print_stats("live_feed|session|orderbook|adapters|paper_live|book_gate|time_integrity|strategy", 14)
            report["where_the_time_goes"]["by_cumulative_time_core_modules"] = [l[:170] for l in buf2.getvalue().splitlines() if l.strip()][:24]
        return report


def verdict(report: dict) -> dict:
    v = report["venues"]
    stale = sum(x["stale_book_transitions"] + x["stale_samples"] for x in v.values())
    dropped = sum(max(0, x["dropped_frames"]) for x in v.values())
    disorder = sum(x["gaps"] + x["out_of_order"] + x["duplicates"] + x["normalization_errors"] for x in v.values())
    resynced = sum(x["connections"] - 1 + x["arrival_stats"].get("backlog_resyncs", 0) for x in v.values())
    behind = max((x["end_to_end_lag_ms"]["p99"] or 0) for x in v.values())            # ground truth: scheduled arrival to the end of processing (the handoff lag no longer shows a backlog once frames are taken at arrival)
    checks = {"stale_book_events_zero": stale == 0, "no_dropped_frames": dropped == 0, "no_gaps_out_of_order_or_errors": disorder == 0, "no_reconnect_or_backlog_resync": resynced == 0, "kept_up_with_the_schedule_end_to_end_p99_lag_under_100ms": behind < 100.0,
              "book_gate_open_at_end": not report["book_gate_suspended_at_end"]}
    return {"checks": checks, "pass": all(checks.values()), "stale_book_events": stale, "dropped_frames": dropped, "gaps_out_of_order_duplicates_errors": disorder}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rate", type=float, default=10_000.0, help="book events per second per venue")
    ap.add_argument("--seconds", type=float, default=5.0)
    ap.add_argument("--out", default=None)
    ap.add_argument("--arrival-stamp", choices=("on", "off"), default="on")
    ap.add_argument("--profile-on-fail", action="store_true", help="when the run fails, repeat 2 s at the same rate under cProfile and report where the time goes")
    args = ap.parse_args()
    global ARRIVAL_STAMP
    ARRIVAL_STAMP = args.arrival_stamp == "on"
    report = asyncio.run(one_run(args.rate, args.seconds))
    report["verdict"] = verdict(report)
    report["arrival_stamp"] = args.arrival_stamp
    report["label"] = "EXPLORATORY: this Mac, one process, a synthetic top-of-book stream through the live parse, book and session path on the wall clock"
    report["machine"] = {"platform": platform.platform(), "python": platform.python_version(), "processor": platform.processor()}
    if not report["verdict"]["pass"] and args.profile_on_fail:
        report["profile_run"] = asyncio.run(one_run(args.rate, 2.0, profile_seconds=2.0)).get("where_the_time_goes")
    text = json.dumps(report, indent=1, default=str)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text)
    print(text)
    return 0 if report["verdict"]["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
