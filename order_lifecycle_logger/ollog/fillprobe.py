"""`ollog fillprobe`: public capture + at-touch probe orders for the perpetual markets, in one process so they share clocks.

    testnet (default):  python3.12 -m ollog fillprobe --markets bybit_linear,binance_futures --coins-list ETHUSDT,XRPUSDT --max-orders 120
    mainnet capture only (no orders, no keys):  ... --network mainnet --capture-only --duration-s 600
    mainnet orders: refused unless --confirm-mainnet <phrase>, OLLOG_MAINNET=1 and inside the hard limits in mainnet.py.

Output directory: capture/ (backtester Tardis layout, plus _raw/), <market>_<stamp>/ (events.jsonl, orders.jsonl, meta.json per
market), capture_notes.jsonl, recon_checks.jsonl and fillprobe_meta.json.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
from decimal import Decimal
from pathlib import Path

from .capture import BinanceCapture, BybitCapture, TardisWriter, stream_url
from .endpoints import LABEL, MARKETS, PUBLIC_WS, assert_testnet_url
from .probe import ProbeConfig, ProbeRunner
from .recorder import Recorder, utc_tag
from .runner import HARD_MAX_ORDERS, Budget
from .venues import make_client, ssl_context

PERP_MARKETS = ("bybit_linear", "binance_futures")


def _table(network: str, market: str) -> dict:
    if network == "mainnet":
        from .mainnet import MAINNET_MARKETS
        return MAINNET_MARKETS[market]
    return MARKETS[market]


def _public_ws(network: str, market: str) -> str:
    if network == "mainnet":
        from .mainnet import PUBLIC_WS_MAINNET
        return PUBLIC_WS_MAINNET[market]
    return PUBLIC_WS[market]


def _guard(network: str):
    if network == "mainnet":
        from .mainnet import assert_mainnet_url
        return assert_mainnet_url
    return assert_testnet_url


def add_arguments(s) -> None:
    s.add_argument("--network", choices=("testnet", "mainnet"), default="testnet")
    s.add_argument("--markets", default=",".join(PERP_MARKETS), help=f"comma list of {PERP_MARKETS}")
    s.add_argument("--coins-list", required=True, help="symbols to capture and probe, e.g. ETHUSDT,XRPUSDT")
    s.add_argument("--capture-only", action="store_true", help="public data capture only: no credentials, no orders")
    s.add_argument("--duration-s", type=float, default=600.0, help="capture-only run length")
    s.add_argument("--max-orders", type=int, default=100, help=f"hard cap on probe orders in the whole run (max {HARD_MAX_ORDERS})")
    s.add_argument("--hold-min-s", type=float, default=15.0)
    s.add_argument("--hold-max-s", type=float, default=45.0)
    s.add_argument("--spacing-s", type=float, default=1.0)
    s.add_argument("--max-notional", type=Decimal, default=Decimal(100), help="refuse symbols whose minimum order exceeds this (quote)")
    s.add_argument("--max-net-notional", type=Decimal, default=Decimal(150), help="|net position| (quote) above which only the reducing side is quoted")
    s.add_argument("--no-preflight", action="store_true", help="skip the far-from-touch order that proves the private stream delivers order events")
    s.add_argument("--no-flatten", action="store_true", help="leave any position the probes built (default: reduce-only market close at the end)")
    s.add_argument("--check-every-s", type=float, default=30.0, help="REST depth reconciliation interval (0 = off)")
    s.add_argument("--max-consecutive-errors", type=int, default=5)
    s.add_argument("--seed", type=int, default=1)
    s.add_argument("--confirm-mainnet", default=None, help="mainnet orders only: the exact confirmation phrase printed by --help of mainnet.py")
    s.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "fillprobe_runs"))


async def run_fillprobe(args) -> int:
    import aiohttp
    network = args.network
    markets = [m.strip() for m in args.markets.split(",") if m.strip()]
    bad = [m for m in markets if m not in PERP_MARKETS]
    if bad:
        raise SystemExit(f"fillprobe covers the perpetual markets only; unknown/unsupported: {bad}")
    symbols = [x.strip() for x in args.coins_list.split(",") if x.strip()]
    keys: dict = {}
    if not args.capture_only:
        if network == "mainnet":
            from .mainnet import violations
            v = violations(markets, symbols, args.max_orders, args.max_notional, args.max_net_notional, args.confirm_mainnet, os.environ.get("OLLOG_MAINNET"))
            if v:
                print("mainnet ORDER run refused (nothing was sent):\n  " + "\n  ".join(v), file=sys.stderr)
                return 2
        missing = []
        for m in markets:
            k, sec = (os.environ.get(n) for n in _table(network, m)["env"])
            if not k or not sec:
                missing.append(f"{m}: {_table(network, m)['env'][0]} / {_table(network, m)['env'][1]}")
            keys[m] = (k, sec)
        if missing:
            print(f"missing {network} credentials in the environment (nothing was sent):\n  " + "\n  ".join(missing), file=sys.stderr)
            return 2
    out = Path(args.out) / f"fillprobe_{network}_{utc_tag()}"
    out.mkdir(parents=True, exist_ok=False)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    notes_f, checks_f = (out / "capture_notes.jsonl").open("a"), (out / "recon_checks.jsonl").open("a")
    note = lambda o: (notes_f.write(json.dumps(o, default=str) + "\n"), notes_f.flush())
    check = lambda o: (checks_f.write(json.dumps(o, default=str) + "\n"), checks_f.flush())
    guard = _guard(network)
    started = time.time()
    meta = {"label": LABEL if network == "testnet" else "MAINNET", "network": network, "markets": markets, "symbols": symbols,
            "capture_only": bool(args.capture_only), "args": {k: str(v) for k, v in vars(args).items() if k != "confirm_mainnet"},
            "started_wall": started}
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_context()), timeout=aiohttp.ClientTimeout(total=15)) as session:
        tables, captures, writers, cap_tasks = {}, {}, {}, []
        for m in markets:
            c = make_client(m, session, network=network)
            tables[m] = await c.instruments()
            miss = [s for s in symbols if s not in tables[m]]
            if miss:
                print(f"{m}: not tradable (post-only, USDT, TRADING): {miss}", file=sys.stderr)
                return 1
            cls = BybitCapture if m == "bybit_linear" else BinanceCapture
            writers[m] = TardisWriter(out / "capture", cls.venue)
            captures[m] = cls(symbols, writers[m], note, check, guard, _table(network, m)["rest"],
                              stream_url(m, symbols, _public_ws(network, m)), session=session)
            cap_tasks.append(asyncio.create_task(captures[m].run(args.check_every_s)))
        flusher = asyncio.create_task(_flush_loop(writers.values(), stop))
        try:
            ready = await _wait_valid(captures, symbols, 60.0, stop)
            if not ready:
                print("capture books did not become valid within 60 s; aborting", file=sys.stderr)
                return 1
            print(f"[{meta['label']}] capture live on {markets} for {symbols}; output {out}")
            results, runners = [], []
            if args.capture_only:
                try:
                    await asyncio.wait_for(stop.wait(), args.duration_s)
                except asyncio.TimeoutError:
                    pass
            else:
                cfg = ProbeConfig(hold_min_s=args.hold_min_s, hold_max_s=args.hold_max_s, spacing_s=args.spacing_s, max_notional=args.max_notional,
                                  max_net_notional=args.max_net_notional, flatten=not args.no_flatten, preflight=not args.no_preflight, max_consecutive_errors=args.max_consecutive_errors,
                                  seed=args.seed, tag=format(int(time.time()) % (36 ** 6), "x").rjust(6, "0"))
                budget = Budget(args.max_orders)
                recs = []
                for m in markets:
                    rec = Recorder(out, m)
                    rec.meta({"tool": "ollog fillprobe", "network": network, "market": m, "symbols": symbols,
                              "instruments": {s: {"tick": str(tables[m][s].tick), "step": str(tables[m][s].step), "min_qty": str(tables[m][s].min_qty),
                                                  "min_notional": str(tables[m][s].min_notional)} for s in symbols}})
                    client = make_client(m, session, *keys[m], network=network)
                    runners.append(ProbeRunner(client, rec, symbols, tables[m], cfg, budget, stop, captures[m]))
                    recs.append(rec)
                    print(f"  {m}: {rec.dir}")
                results = await asyncio.gather(*(r.run() for r in runners), return_exceptions=True)
                for r, rec, res in zip(runners, recs, results):
                    rec.close()
                    tail = f" ({type(res).__name__}: {res})" if isinstance(res, BaseException) else ""
                    print(f"{r.market}: {r.stats['orders']} orders, {r.stats['filled_orders']} filled, {r.stats['post_only_rejects']} post-only rejects, "
                          f"{r.stats['errors']} errors; stopped: {r.stop_reason}{tail}")
                await asyncio.sleep(5.0)                                   # trailing public trades for the last orders
        finally:
            for c in captures.values():
                c.stop.set()
            await asyncio.gather(*cap_tasks, return_exceptions=True)
            flusher.cancel()
            for w in writers.values():
                w.flush()
                w.close()
            notes_f.close()
            checks_f.close()
        meta.update(ended_wall=time.time(), capture={m: dict(c.counts) for m, c in captures.items()},
                    runners={r.market: {"stats": r.stats, "stop_reason": r.stop_reason, "flatten": r.flatten_results} for r in runners})
        (out / "fillprobe_meta.json").write_text(json.dumps(meta, indent=2, default=str))
        for m, c in captures.items():
            k = c.counts
            print(f"{m}: capture frames {k['frames']}, snapshots {k['snapshots']}, level updates {k['levels']}, trades {k['trades']}, "
                  f"gaps {k['note_gap']}, resyncs {k['syncs']}, REST checks {k['checks']} (exact match {k['checks_match']})")
    return 0


async def _flush_loop(writers, stop) -> None:
    while True:
        await asyncio.sleep(1.0)
        for w in writers:
            w.flush()


async def _wait_valid(captures, symbols, timeout: float, stop) -> bool:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout and not stop.is_set():
        if all(c.valid.get(s) for c in captures.values() for s in symbols):
            return True
        await asyncio.sleep(0.5)
    return False
