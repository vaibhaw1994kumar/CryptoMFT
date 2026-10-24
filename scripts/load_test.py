"""Load test of the live market-data path with recorded websocket frames (audit Step 13).

Replays a capture's recorded order-book frames through ``LiveOrderBookService`` as
fast as the process can take them: raw capture, parse, normalise, validate, book
apply, timing monitor, gate and recording, exactly as live. Reports throughput,
per-frame processing time and (with --memory) traced memory after each pass.

    PYTHONPATH=src python scripts/load_test.py --capture data/live/<session> \\
        --config config/perp_research.json --venue BYBIT --passes 6 [--durable] [--memory]

Each pass replays the whole capture once with its timestamps shifted forward, so a
long run behaves like one continuous session. Numbers are for this host only.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import sys
import tempfile
import time
import tracemalloc

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from crypto_stat_arb.live_feed import LiveFeedSettings, LiveOrderBookService  # noqa: E402


def load_frames(capture: Path, venue: str) -> list[dict]:
    prefix = "orderbook." if venue == "BYBIT" else "depth"
    frames = []
    for part in sorted(capture.glob("part-*")):
        with (part / "wire.jsonl").open(encoding="utf-8") as handle:
            for line in handle:
                row = json.loads(line)
                if row["venue"] == venue and row["channel"].startswith(prefix):
                    frames.append(row)
    if not frames:
        raise SystemExit(f"no {venue} order-book frames in {capture}")
    return frames


def run(capture: Path, config: Path, venue: str, passes: int, durable: bool, memory: bool) -> dict:
    frames = load_frames(capture, venue)
    linear = "linear" in config.read_text()
    timings: list[int] = []
    traced_mb: list[float] = []
    with tempfile.TemporaryDirectory() as directory:
        clock = {"ns": frames[0]["receive_ts_ns"]}
        service = LiveOrderBookService(
            config, Path(directory), "load-test",
            LiveFeedSettings(venues=(venue,), binance_market="linear" if linear else "spot",
                             bybit_market="linear" if linear else "spot", durable=durable),
            clock_ns=lambda: clock["ns"],
        )
        if memory:
            tracemalloc.start()
        span = frames[-1]["receive_ts_ns"] - frames[0]["receive_ts_ns"] + 1_000_000_000
        started = time.perf_counter()
        for index in range(passes):
            shift = index * span
            for frame in frames:
                clock["ns"] = frame["receive_ts_ns"] + shift
                before = time.perf_counter_ns()
                service._process_websocket_frame(venue, "wss://load-test", frame["frame_data"],
                                                 channel=frame["channel"])
                if not memory:                  # the timings list would itself grow the traced memory
                    timings.append(time.perf_counter_ns() - before)
            if memory:
                traced_mb.append(round(tracemalloc.get_traced_memory()[0] / 1e6, 2))
            service.books[venue].start_resync("load-test pass boundary")   # each pass starts at its snapshot
        elapsed = time.perf_counter() - started
        book = service.books[venue]
        stats = book.stats
        result_book = {"gaps": stats.gaps, "deltas_applied": stats.deltas_applied, "snapshots": stats.snapshots}
        service.recorder.close({})
        service.ohlc_writer.close()
        service.ltp_writer.close()
    timings.sort()
    frames_done = len(frames) * passes

    def pct(q: float) -> float | None:
        if not timings:
            return None
        return round(timings[min(len(timings) - 1, int(len(timings) * q))] / 1e3, 1)

    return {
        "capture": str(capture), "venue": venue, "frames_per_pass": len(frames), "passes": passes,
        "durable": durable, "traced_memory": memory,
        "frames": frames_done, "frames_per_second": round(frames_done / elapsed),
        "per_frame_us": {"p50": pct(0.5), "p99": pct(0.99), "p99_9": pct(0.999),
                         "max": round(timings[-1] / 1e3, 1) if timings else None},
        "recorded_live_rate_per_second": round(len(frames) / ((frames[-1]["receive_ts_ns"] - frames[0]["receive_ts_ns"]) / 1e9), 1),
        "traced_mb_after_each_pass": traced_mb,
        "book": result_book,
        "host": platform.platform(), "python": platform.python_version(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--venue", choices=("BINANCE", "BYBIT"), required=True)
    parser.add_argument("--passes", type=int, default=3)
    parser.add_argument("--durable", action="store_true", help="fsync every record, as live captures do")
    parser.add_argument("--memory", action="store_true", help="trace memory (slows processing several-fold)")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run(args.capture, args.config, args.venue, args.passes, args.durable, args.memory)
    text = json.dumps(report, indent=2)
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
