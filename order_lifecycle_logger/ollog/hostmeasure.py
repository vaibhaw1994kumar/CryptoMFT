"""`ollog hostmeasure`: the production-host measurement run (owner order 20 Nov 2026, Phase 2 F03 d). PUBLIC mainnet market data only:
no credentials, no orders. The owner runs it on the production host; it replaces the `singapore` latency preset and the pre-12 Nov
measurements, which stay ASSUMED ("pre-migration") until a run of this exists.

    python3.12 -m ollog hostmeasure --hours 24 --coins-list SOLUSDT          # record (default 24 h)
    python3.12 -m ollog hostmeasure-derive <run dir> [--src <dir with crypto_stat_arb>]   # figures -> <run dir>/hostmeasure_summary.json

One process records, on the same clocks (wall and monotonic, back to back):
  clock.jsonl          `chronyc tracking` every --chrony-every-s (default 60 s): the raw text, the exit code, the read stamps. The text is kept
                       raw; the derive step parses it with the platform's own parser (crypto_stat_arb.time_integrity.parse_chronyc_tracking),
                       so the parse is checked on real output. A host without a working chronyc records the failure (the gate's "unknown").
  rest_latency.jsonl   one GET of each venue's server-time endpoint every --rest-every-s (default 10 s): send/return stamps and the server
                       time (Bybit timeNano, Binance serverTime ms): REST round trip and the venue clock against the host.
  fillprobe_mainnet_*/ the public book, trade and top-of-book (bbo) capture of `ollog fillprobe --network mainnet --capture-only --fast-streams`,
                       every record with the venue's event time and the local receive stamps.

The derive step reports: chrony offset / uncertainty / stratum / sync share; REST round trip per venue; websocket latency per venue and stream
(receive minus the venue's event time, raw and corrected by the chrony offset in force); the book publication interval per venue (gaps between
the venue's own event times); and cross-venue lead-lag of the top-of-book mid (which venue's mid moves first, by how much, on local receive time,
where the host clock offset cancels, and on venue event time).
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import platform
import subprocess
import sys
import time
from bisect import bisect_left, bisect_right
from pathlib import Path

from .models import stamp
from .recorder import utc_tag

LABEL = "MEASURED:hostmeasure"
SERVER_TIME_PATHS = {"bybit_linear": "/v5/market/time", "binance_futures": "/fapi/v1/time"}
PERP_MARKETS = ("bybit_linear", "binance_futures")


def add_arguments(s) -> None:
    s.add_argument("--coins-list", default="SOLUSDT", help="symbols to capture on both venues (default SOLUSDT)")
    s.add_argument("--markets", default=",".join(PERP_MARKETS))
    s.add_argument("--hours", type=float, default=24.0, help="run length in hours (default 24)")
    s.add_argument("--chrony-every-s", type=float, default=60.0)
    s.add_argument("--rest-every-s", type=float, default=10.0)
    s.add_argument("--chronyc", default="chronyc", help="path of the chronyc binary")
    s.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "hostmeasure_runs"))


def add_derive_arguments(s) -> None:
    s.add_argument("dir", help="a hostmeasure_* run directory")
    s.add_argument("--src", default=str(Path(__file__).resolve().parents[2] / "crypto_MFT_arb_platform" / "src"),
                   help="directory that contains crypto_stat_arb (for the chrony parser)")
    s.add_argument("--min-move-bps", type=float, default=0.5, help="lead-lag: a top-of-book mid change at least this large is a move")
    s.add_argument("--window-ms", type=float, default=500.0, help="lead-lag: the other venue's same-direction move must come within this")


# ---- recording ------------------------------------------------------------------------------------------------------------------------------
def run_chronyc(binary: str = "chronyc", timeout_s: float = 5.0) -> tuple[int | None, str, str]:
    """(exit code, stdout, stderr) of `chronyc tracking`. A missing or broken binary is a record, not an exception (exit code None)."""
    try:
        r = subprocess.run([binary, "tracking"], capture_output=True, text=True, timeout=timeout_s)
        return r.returncode, r.stdout, r.stderr[-500:]
    except (OSError, subprocess.SubprocessError) as exc:
        return None, "", f"{type(exc).__name__}: {exc}"[:500]


async def chrony_loop(write, stop: asyncio.Event, every_s: float, runner=run_chronyc) -> int:
    n = 0
    while not stop.is_set():
        t0 = stamp()
        rc, text, err = await asyncio.to_thread(runner)
        t1 = stamp()
        write({"t_wall_ns": t0[0], "t_mono_ns": t0[1], "t_ret_wall_ns": t1[0], "t_ret_mono_ns": t1[1], "rc": rc, "text": text, "stderr": err})
        n += 1
        try:
            await asyncio.wait_for(stop.wait(), every_s)
        except asyncio.TimeoutError:
            pass
    return n


def server_time_ns(market: str, obj: dict) -> int:
    if market == "bybit_linear":
        nano = (obj.get("result") or {}).get("timeNano")
        return int(nano) if nano else int(obj["time"]) * 1_000_000
    return int(obj["serverTime"]) * 1_000_000


async def rest_loop(clients: dict, write, stop: asyncio.Event, every_s: float) -> int:
    """`clients`: market -> an ollog venue client (its `_http` checks the host against the mainnet allow-list)."""
    n = 0
    while not stop.is_set():
        for market, c in clients.items():
            rec = {"market": market}
            try:
                status, obj, t_send, t_ret = await c._http("GET", f"{c.cfg['rest']}{SERVER_TIME_PATHS[market]}")
                rec.update(ok=status == 200, status=status, t_send_wall_ns=t_send[0], t_send_mono_ns=t_send[1], t_ret_wall_ns=t_ret[0], t_ret_mono_ns=t_ret[1],
                           server_ns=server_time_ns(market, obj))
            except Exception as exc:
                rec.update(ok=False, error=f"{type(exc).__name__}: {exc}"[:300])
            write(rec)
            n += 1
        try:
            await asyncio.wait_for(stop.wait(), every_s)
        except asyncio.TimeoutError:
            pass
    return n


def _appender(path: Path):
    f = path.open("a")

    def write(obj):
        f.write(json.dumps(obj, separators=(",", ":")) + "\n")
        f.flush()
    return write, f


async def run_hostmeasure(args) -> int:
    import aiohttp
    from . import fillprobe as fp
    from .venues import make_client, ssl_context

    markets = [m.strip() for m in args.markets.split(",") if m.strip()]
    out = Path(args.out) / f"hostmeasure_{utc_tag()}"
    out.mkdir(parents=True, exist_ok=False)
    meta = {"label": LABEL, "tool": "ollog hostmeasure", "network": "mainnet PUBLIC market data (no keys, no orders)", "host": platform.node(), "platform": platform.platform(),
            "python": sys.version.split()[0], "markets": markets, "symbols": args.coins_list, "hours": args.hours, "chrony_every_s": args.chrony_every_s,
            "rest_every_s": args.rest_every_s, "started_wall_ns": time.time_ns(), "started_mono_ns": time.monotonic_ns()}
    (out / "hostmeasure_meta.json").write_text(json.dumps(meta, indent=1))
    p = argparse.ArgumentParser()
    fp.add_arguments(p)
    fp_args = p.parse_args(["--network", "mainnet", "--capture-only", "--fast-streams", "--markets", ",".join(markets), "--coins-list", args.coins_list,
                            "--duration-s", str(args.hours * 3600.0), "--out", str(out)])
    stop = asyncio.Event()
    w_clock, f_clock = _appender(out / "clock.jsonl")
    w_rest, f_rest = _appender(out / "rest_latency.jsonl")
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_context()), timeout=aiohttp.ClientTimeout(total=15)) as session:
        clients = {m: make_client(m, session, network="mainnet") for m in markets}
        capture = asyncio.create_task(fp.run_fillprobe(fp_args))
        loops = [asyncio.create_task(chrony_loop(w_clock, stop, args.chrony_every_s, lambda: run_chronyc(args.chronyc))),
                 asyncio.create_task(rest_loop(clients, w_rest, stop, args.rest_every_s))]
        try:
            rc = await capture
        finally:
            stop.set()
            counts = await asyncio.gather(*loops, return_exceptions=True)
            f_clock.close()
            f_rest.close()
    meta.update(ended_wall_ns=time.time_ns(), ended_mono_ns=time.monotonic_ns(), capture_rc=rc, clock_records=counts[0], rest_records=counts[1])
    (out / "hostmeasure_meta.json").write_text(json.dumps(meta, indent=1, default=str))
    print(f"[{LABEL}] {out}: capture rc {rc}, clock records {counts[0]}, REST probes {counts[1]}. Next: python3.12 -m ollog hostmeasure-derive {out}")
    return rc


# ---- derive ---------------------------------------------------------------------------------------------------------------------------------
def _q(values: list[float], qs=(0.5, 0.9, 0.99)) -> dict | None:
    if not values:
        return None
    v = sorted(values)
    out = {f"p{int(q * 100)}": round(v[min(len(v) - 1, int((len(v) - 1) * q))], 3) for q in qs}
    out.update(n=len(v), min=round(v[0], 3), max=round(v[-1], 3))
    return out


def _jsonl(path: Path):
    if not path.exists():
        return
    with path.open() as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def _capture_records(run: Path, venue: str, stream: str):
    """Every record of one venue and stream (book event, trade, bbo) over every capture directory of the run, per pair, in file order."""
    layout = {"event": "book_events", "trade": "trades", "bbo": "bbo"}[stream]
    out: dict[str, list] = {}
    for d in sorted(run.glob("fillprobe_*/capture")) + ([run / "capture"] if (run / "capture").exists() else []):
        for pair_dir in sorted((d / venue / layout).glob("pair=*")):
            pair = pair_dir.name.split("=", 1)[1]
            for f in sorted(pair_dir.glob("date=*/*.jsonl.gz"), key=lambda x: (x.parent.name, int(x.stem.split(".")[0]))):
                with gzip.open(f, "rt") as fh:
                    out.setdefault(pair, []).extend(json.loads(x) for x in fh if x.strip())
    return out


def _chrony_series(run: Path, src: str | None) -> tuple[list[tuple[int, int | None]], dict]:
    """[(read wall ns, offset ns or None)] in time order, and the clock block of the summary."""
    if src and src not in sys.path:
        sys.path.insert(0, src)
    from crypto_stat_arb.time_integrity import chrony_measurement, parse_chronyc_tracking
    series, offsets, uncs, strata, ages, unsynced, failed, refs = [], [], [], set(), [], 0, 0, set()
    for r in _jsonl(run / "clock.jsonl"):
        if r.get("rc") != 0 or not r.get("text"):
            failed += 1
            series.append((r["t_wall_ns"], None))
            continue
        fields = parse_chronyc_tracking(r["text"])
        m = chrony_measurement(fields)
        if m is None:
            unsynced += 1
            series.append((r["t_wall_ns"], None))
            continue
        series.append((r["t_wall_ns"], m.offset_ns))
        offsets.append(m.offset_ns / 1e6)
        uncs.append(m.uncertainty_ns / 1e6)
        strata.add(fields.get("stratum"))
        refs.add(fields.get("reference_name") or fields.get("reference_id"))
        ages.append((r["t_wall_ns"] - m.measured_ts_ns) / 1e9)
    total = len(series)
    block = {"records": total, "chronyc_failed": failed, "unsynchronised": unsynced, "synchronised_share": round((total - failed - unsynced) / total, 4) if total else None,
             "offset_ms": _q(offsets), "abs_offset_ms": _q([abs(x) for x in offsets]), "uncertainty_ms": _q(uncs), "last_update_age_s": _q(ages),
             "strata": sorted(s for s in strata if s is not None), "references": sorted(str(x) for x in refs if x),
             "uncertainty_rule": "|system offset| + root dispersion + root delay / 2 (owner 20 Nov)"}
    return series, block


def _offset_at(series: list[tuple[int, int | None]], t_ns: int, max_age_ns: int = 900_000_000_000) -> int | None:
    i = bisect_right(series, (t_ns, float("inf"))) - 1
    if i < 0 or series[i][1] is None or t_ns - series[i][0] > max_age_ns:
        return None
    return series[i][1]


def _rest_block(run: Path, series) -> dict:
    by: dict[str, dict[str, list]] = {}
    for r in _jsonl(run / "rest_latency.jsonl"):
        b = by.setdefault(r["market"], {"rtt": [], "venue_minus_host": [], "venue_minus_ntp": [], "errors": 0})
        if not r.get("ok"):
            b["errors"] += 1
            continue
        rtt = (r["t_ret_mono_ns"] - r["t_send_mono_ns"]) / 1e6
        mid_wall = (r["t_send_wall_ns"] + r["t_ret_wall_ns"]) // 2
        b["rtt"].append(rtt)
        b["venue_minus_host"].append((r["server_ns"] - mid_wall) / 1e6)
        off = _offset_at(series, mid_wall)
        if off is not None:
            b["venue_minus_ntp"].append((r["server_ns"] - (mid_wall - off)) / 1e6)
    return {m: {"round_trip_ms": _q(b["rtt"]), "venue_clock_minus_host_ms": _q(b["venue_minus_host"]), "venue_clock_minus_ntp_ms": _q(b["venue_minus_ntp"]),
                "errors": b["errors"], "note": "venue clock figures carry +/- half the round trip"} for m, b in sorted(by.items())}


def _ws_and_interval(run: Path, series) -> tuple[dict, dict]:
    lat, gaps = {}, {}
    for venue in ("bybit", "binance"):
        for stream in ("event", "trade", "bbo"):
            raw, corrected, uncorrected_n, gap = [], [], 0, []
            for pair, recs in _capture_records(run, venue, stream).items():
                last_ex = None
                for r in recs:
                    ex = r.get("exchange_ts", r.get("timestamp"))
                    if ex is None or r.get("local_ts") is None:
                        continue
                    local_ns, ex_ns = int(r["local_ts"] * 1e9), int(ex * 1e9)
                    raw.append((local_ns - ex_ns) / 1e6)
                    off = _offset_at(series, local_ns)
                    if off is None:
                        uncorrected_n += 1
                    else:
                        corrected.append((local_ns - off - ex_ns) / 1e6)
                    if stream != "trade" and ex_ns != last_ex:
                        if last_ex is not None and ex_ns > last_ex:
                            gap.append((ex_ns - last_ex) / 1e6)
                        last_ex = ex_ns
            if raw:
                lat[f"{venue}:{stream}"] = {"receive_minus_event_ms": _q(raw), "corrected_by_chrony_ms": _q(corrected), "records_without_a_chrony_offset": uncorrected_n}
            if gap:
                gaps[f"{venue}:{stream}"] = {"publication_interval_ms": _q(gap), "rule": "gaps between consecutive distinct venue event times (one pair's stream)"}
    return lat, gaps


def _moves(recs, time_key: str, min_move_bps: float) -> list[tuple[int, int]]:
    """(time ns, direction +1/-1) of each top-of-book mid change of at least min_move_bps from the last move's mid."""
    out, ref = [], None
    for r in recs:
        if r.get("bid") is None or r.get("ask") is None or r.get(time_key) is None:
            continue
        mid = (float(r["bid"]) + float(r["ask"])) / 2.0
        if ref is None:
            ref = mid
            continue
        bps = (mid - ref) / ref * 1e4
        if abs(bps) >= min_move_bps:
            out.append((int(r[time_key] * 1e9), 1 if bps > 0 else -1))
            ref = mid
    out.sort()
    return out


def lead_lag(a: list[tuple[int, int]], b: list[tuple[int, int]], window_ns: int) -> dict:
    """For each move of venue A, the nearest same-direction move of venue B within +/- window: lag = t_B - t_A (positive: A first). Each B move is used once."""
    used, lags = set(), []
    tb = [t for t, _ in b]
    for t, d in a:
        lo, hi = bisect_left(tb, t - window_ns), bisect_right(tb, t + window_ns)
        best = None
        for j in range(lo, hi):
            if b[j][1] == d and j not in used and (best is None or abs(tb[j] - t) < abs(tb[best] - t)):
                best = j
        if best is not None:
            used.add(best)
            lags.append((tb[best] - t) / 1e6)
    first_a = sum(1 for x in lags if x > 0)
    first_b = sum(1 for x in lags if x < 0)
    return {"moves_a": len(a), "moves_b": len(b), "matched": len(lags), "a_first": first_a, "b_first": first_b, "same_time": len(lags) - first_a - first_b,
            "a_first_share": round(first_a / len(lags), 4) if lags else None, "lag_b_minus_a_ms": _q(lags), "abs_lag_ms": _q([abs(x) for x in lags])}


def derive(run: Path, src: str | None = None, min_move_bps: float = 0.5, window_ms: float = 500.0) -> dict:
    run = Path(run)
    meta = json.loads((run / "hostmeasure_meta.json").read_text()) if (run / "hostmeasure_meta.json").exists() else {}
    series, clock = _chrony_series(run, src)
    ws, intervals = _ws_and_interval(run, series)
    by_bybit, by_binance = _capture_records(run, "bybit", "bbo"), _capture_records(run, "binance", "bbo")
    ll = {}
    for pair in sorted(set(by_bybit) & set(by_binance)):
        ll[pair] = {"A": "bybit", "B": "binance",
                    "on_local_receive_time": lead_lag(_moves(by_bybit[pair], "local_ts", min_move_bps), _moves(by_binance[pair], "local_ts", min_move_bps), int(window_ms * 1e6)),
                    "on_venue_event_time": lead_lag(_moves(by_bybit[pair], "exchange_ts", min_move_bps), _moves(by_binance[pair], "exchange_ts", min_move_bps), int(window_ms * 1e6)),
                    "min_move_bps": min_move_bps, "window_ms": window_ms}
    summary = {"label": LABEL, "host": meta.get("host"), "started_wall_ns": meta.get("started_wall_ns"), "ended_wall_ns": meta.get("ended_wall_ns"), "hours": meta.get("hours"),
               "clock": clock, "rest": _rest_block(run, series), "websocket_latency": ws, "book_publication_interval": intervals, "lead_lag": ll,
               "replaces": "the singapore latency preset and the pre-12 Nov measurements (labelled ASSUMED, pre-migration, until this run exists)"}
    (run / "hostmeasure_summary.json").write_text(json.dumps(summary, indent=1))
    return summary
