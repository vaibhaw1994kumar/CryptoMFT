from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import time
from decimal import Decimal
from pathlib import Path

from .endpoints import LABEL, MARKETS
from .pricing import NoOrder, min_quantity, passive_buy_price
from .recorder import Recorder
from .runner import HARD_MAX_ORDERS, Budget, MarketRunner, RunConfig
from .summary import summarize
from .universe import choose, zip_coin_activity
from .venues import make_client, ssl_context

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ZIP = str(Path.home() / "Downloads" / "bybit_30d.zip")
ALL_MARKETS = tuple(MARKETS)

ENV_HELP = "\n".join(f"  {m:<16} {MARKETS[m]['env'][0]}, {MARKETS[m]['env'][1]}" for m in ALL_MARKETS)
EPILOG = f"""TESTNET ONLY. The hosts are hard-coded and checked on every request; there is no option to change them.

Credentials come only from these environment variables (create testnet keys at testnet.bybit.com, testnet.binance.vision and
testnet.binancefuture.com; Bybit uses one pair for spot and linear):
{ENV_HELP}

  python3.12 -m ollog discover                       public data only: coin choice, filters, planned orders, clock probe
  python3.12 -m ollog run --markets bybit_spot,bybit_linear,binance_spot,binance_futures --coins 5 --max-orders 200
"""


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ollog", description="Testnet order-lifecycle latency logger.", epilog=EPILOG,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("discover", "run"):
        s = sub.add_parser(name, formatter_class=argparse.RawDescriptionHelpFormatter, epilog=EPILOG)
        s.add_argument("--markets", default=",".join(ALL_MARKETS), help=f"comma list of {ALL_MARKETS}")
        s.add_argument("--coins", type=int, default=5, help="how many coins (default 5)")
        s.add_argument("--coins-list", default="", help="explicit symbols, e.g. SOLUSDT,XRPUSDT (still must pass the intersection)")
        s.add_argument("--zip", default=DEFAULT_ZIP, help="Bybit export whose file names give the 22 coins (directory read only)")
        s.add_argument("--dist-bps", type=Decimal, default=Decimal(50), help="buy this many bps below the best bid")
        s.add_argument("--cap-margin-bps", type=Decimal, default=Decimal(100),
                       help="price this far under a buy-price limit reported by the venue (its reference price drifts)")
        s.add_argument("--max-notional", type=Decimal, default=Decimal(100), help="refuse symbols whose minimum order exceeds this (quote)")
        if name == "discover":
            s.add_argument("--check-ws", action="store_true", help="also connect to each public testnet stream (no credentials)")
        else:
            s.add_argument("--max-orders", type=int, default=100, help=f"hard cap on orders sent in the whole run (max {HARD_MAX_ORDERS})")
            s.add_argument("--hold-s", type=float, default=1.0)
            s.add_argument("--hold-jitter-s", type=float, default=0.5)
            s.add_argument("--spacing-s", type=float, default=0.5, help="pause between orders in a market (minimum 0.2)")
            s.add_argument("--max-consecutive-errors", type=int, default=5)
            s.add_argument("--reject-probe-every", type=int, default=0, help="every Nth order is a crossing post-only that must be rejected")
            s.add_argument("--sync-every-s", type=float, default=300.0)
            s.add_argument("--seed", type=int, default=1)
            s.add_argument("--out", default=str(ROOT / "latency_logs"))
    from . import fillprobe as _fp
    f = sub.add_parser("fillprobe", help="public capture + at-touch probe orders on the perpetual markets (testnet default; mainnet is gated)")
    _fp.add_arguments(f)
    rp = sub.add_parser("replay", help="replay a fillprobe run through the maker fill engine under several configs and score it")
    from . import replay as _rp
    _rp.add_arguments(rp)
    cb = sub.add_parser("calibrate", help="convert a fillprobe run (real orders + capture) into the backtester's fill_events.jsonl")
    from . import calibrate as _cb
    _cb.add_arguments(cb)
    z = sub.add_parser("summarize", help="rebuild summary.json for a finished run directory")
    z.add_argument("dir")
    return p


def _markets(arg: str) -> list[str]:
    ms = [m.strip() for m in arg.split(",") if m.strip()]
    bad = [m for m in ms if m not in MARKETS]
    if bad:
        raise SystemExit(f"unknown market(s) {bad}; choose from {list(ALL_MARKETS)}")
    return ms


async def _universe(session, markets, args):
    clients = {m: make_client(m, session) for m in markets}
    tables = {}
    for m, c in clients.items():
        tables[m] = await c.instruments()
        print(f"  {m:<16} {len(tables[m])} eligible instruments")
    cands = zip_coin_activity(args.zip) if not args.coins_list else {s: 0 for s in args.coins_list.split(",")}
    chosen, dropped = choose(tables, cands, args.coins)
    return clients, tables, chosen, dropped, len(cands)


async def _discover(args) -> int:
    import aiohttp
    markets = _markets(args.markets)
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_context()), timeout=aiohttp.ClientTimeout(total=15)) as session:
        print(f"[{LABEL}] public endpoints only; nothing is signed or sent to an order endpoint")
        clients, tables, chosen, dropped, ncand = await _universe(session, markets, args)
        print(f"\ncoins in the export: {ncand}; chosen {len(chosen)}: {chosen}")
        for s, why in dropped.items():
            print(f"  dropped {s}: {'; '.join(why)}")
        if not chosen:
            print("no coin is tradable on every selected market")
            return 1
        for m, c in clients.items():
            rtts = []
            for _ in range(5):
                server_ms, w0, w1 = await c.server_time_ms()
                rtts.append(((w1 - w0) / 1e6, server_ms - (w0 + w1) / 2e6))
            rtt, off = min(rtts)
            print(f"\n{m}: clock offset {off:+.1f} ms (+/- {rtt / 2:.1f}), best of 5 probes, rtt {rtt:.1f} ms")
            for sym in chosen:
                inst = tables[m][sym]
                bid, ask = await c.best_bid_ask(sym)
                ref = await c.reference_price(sym) if inst.pct_down is not None else None
                try:
                    price = passive_buy_price(inst, bid, args.dist_bps, ref)
                    qty = min_quantity(inst, price, args.max_notional)
                    plan = f"buy {qty} @ {price} (notional {qty * price:.2f}, {args.dist_bps} bps below bid {bid})"
                except NoOrder as exc:
                    plan = f"WOULD SKIP: {exc}"
                print(f"  {sym:<12} tick {inst.tick} step {inst.step} min_qty {inst.min_qty} min_notional {inst.min_notional} "
                      f"band_down {inst.pct_down} | bid {bid} ask {ask} | {plan}")
        if args.check_ws:
            await _check_ws(markets)
    return 0


async def _check_ws(markets) -> None:
    import websockets
    urls = {"bybit_spot": "wss://stream-testnet.bybit.com/v5/public/spot", "bybit_linear": "wss://stream-testnet.bybit.com/v5/public/linear",
            "binance_spot": "wss://stream.testnet.binance.vision/ws/btcusdt@bookTicker",
            "binance_futures": "wss://fstream.binancefuture.com/ws/btcusdt@bookTicker"}
    from .endpoints import assert_testnet_url
    print("\npublic stream reachability:")
    for m in markets:
        try:
            async with websockets.connect(assert_testnet_url(urls[m]), ssl=ssl_context(), open_timeout=10) as ws:
                if m.startswith("bybit"):
                    await ws.send(json.dumps({"op": "ping"}))
                msg = await asyncio.wait_for(ws.recv(), 10)
                print(f"  {m:<16} ok ({str(msg)[:60]!r})")
        except Exception as exc:
            print(f"  {m:<16} FAILED {type(exc).__name__}: {exc}")


async def _run(args) -> int:
    import aiohttp
    markets = _markets(args.markets)
    keys, missing = {}, []
    for m in markets:
        k, s = (os.environ.get(n) for n in MARKETS[m]["env"])
        if not k or not s:
            missing.append(f"{m}: {MARKETS[m]['env'][0]} / {MARKETS[m]['env'][1]}")
        keys[m] = (k, s)
    if missing:
        print("missing testnet credentials in the environment (nothing was sent):\n  " + "\n  ".join(missing), file=sys.stderr)
        return 2
    cfg = RunConfig(dist_bps=args.dist_bps, hold_s=args.hold_s, hold_jitter_s=args.hold_jitter_s, spacing_s=args.spacing_s,
                    max_consecutive_errors=args.max_consecutive_errors, max_notional=args.max_notional, cap_margin_bps=args.cap_margin_bps,
                    reject_probe_every=args.reject_probe_every, sync_every_s=args.sync_every_s, seed=args.seed,
                    tag=format(int(time.time()) % (36 ** 6), "x").rjust(6, "0"))
    budget, stop = Budget(args.max_orders), asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    out_root = Path(args.out)
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_context()), timeout=aiohttp.ClientTimeout(total=15)) as session:
        _, tables, chosen, dropped, _ = await _universe(session, markets, args)
        if not chosen:
            print("no coin is tradable on every selected market; nothing sent")
            return 1
        print(f"[{LABEL}] coins: {chosen}; dropped: {len(dropped)}; cap {budget.total} orders")
        runners, recs = [], []
        for m in markets:
            rec = Recorder(out_root, m)
            rec.meta({"tool": "ollog", "market": m, "rest": MARKETS[m]["rest"], "ws": MARKETS[m]["ws"], "coins": chosen,
                      "dropped": dropped, "env_var_names": list(MARKETS[m]["env"]), "args": {k: str(v) for k, v in vars(args).items()},
                      "instruments": {s: {"tick": str(tables[m][s].tick), "step": str(tables[m][s].step), "min_qty": str(tables[m][s].min_qty),
                                          "min_notional": str(tables[m][s].min_notional)} for s in chosen}})
            client = make_client(m, session, *keys[m])
            runners.append(MarketRunner(client, rec, chosen, tables[m], cfg, budget, stop))
            recs.append(rec)
            print(f"  {m}: {rec.dir}")
        results = await asyncio.gather(*(r.run() for r in runners), return_exceptions=True)
        for r, rec, res in zip(runners, recs, results):
            rec.close()
            tail = f" ({type(res).__name__}: {res})" if isinstance(res, BaseException) else ""
            print(f"{r.market}: {r.stats['orders']} orders, {r.stats['clean']} clean, {r.stats['errors']} with errors, {r.stats['price_limit_learned']} price-limit rejections; stopped: {r.stop_reason}{tail}")
            if r.stats["orders"]:
                summarize(rec.dir)
    return 0 if all(not isinstance(x, BaseException) for x in results) else 1


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    if args.cmd == "summarize":
        print(json.dumps(summarize(Path(args.dir)), indent=2)[:4000])
        return 0
    if args.cmd == "fillprobe":
        from .fillprobe import run_fillprobe
        return asyncio.run(run_fillprobe(args))
    if args.cmd == "replay":
        from .replay import main as replay_main
        return replay_main(args)
    if args.cmd == "calibrate":
        from .calibrate import main as calibrate_main
        return calibrate_main(args)
    return asyncio.run(_discover(args) if args.cmd == "discover" else _run(args))
