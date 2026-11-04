"""One-fix-at-a-time ladder (owner, 2026-11-05) on the 2-hour March slices 00:00-02:00 UTC of 03-08..03-11, from step 8. Cumulative rows:
 L0 step 8 (v1.1 steps 1-8; hedge move = the legacy-trained -4.285 bid; quotes unrounded; cross uncapped; makers on both venues; hedge on the view book)
 L1 +fix1 hedge move = the PROBE number    L2 +fix2 tick rounding (ticks 0.001/0.001)    L3 +fix3 cross cap to the print    L4 +fix4 Bybit-only makers    L5 +fix5 hedge priced on the book at arrival (truth)
Max 3 workers. usage: qv2_ladder9_pool.py [N]   (waits for hedge_move/probe_result.json before any row from L1 on)"""
import json, os, subprocess, sys, time
from pathlib import Path
X = Path("/Users/vaibhav/Documents/Newproject/out/queue_v2")
RUNS = X / "runs_ladder9"
DAYS = ["2024-03-08", "2024-03-09", "2024-03-10", "2024-03-11"]
PROBE = X / "hedge_move" / "probe_result.json"
LEGACY_BID = -4.2853
REASON = "exploratory: 2-hour slice; Binance March-2024 has bookTicker (L1) only (manifest EMPTY); funding not settled; makers judged against the tape of the venue they rest on"
FIT = str(X / "unknown_queue_fit_mar2024_NONE.json")
TICKS = {"bybit": 0.001, "binance": 0.001}


def rows():
    import calendar, time as t
    probe = json.loads(PROBE.read_text())["probe_bid_move_bps"] if PROBE.exists() else None
    base_arb = {"round_quotes_to_tick": False, "maker_venues": []}
    def r(tag, moves, arb, maker, cfg, ticks=False, needs_probe=False):
        return dict(tag=tag, moves=moves, arb=arb, maker=maker, cfg=cfg, ticks=ticks, needs_probe=needs_probe)
    cap_off = {"opposite_cross_cap_to_print": False}
    out = [r("L0_step8", {"bid": LEGACY_BID, "ask": 0.0}, base_arb, {**cap_off}, {"hedge_arrival_book": "view"})]
    pm = {"bid": probe, "ask": 0.0}
    out.append(r("L1_hedge_move_probe", pm, base_arb, {**cap_off}, {"hedge_arrival_book": "view"}, needs_probe=True))
    out.append(r("L2_tick_rounding", pm, {**base_arb, "round_quotes_to_tick": True}, {**cap_off}, {"hedge_arrival_book": "view"}, ticks=True, needs_probe=True))
    out.append(r("L3_cross_cap", pm, {**base_arb, "round_quotes_to_tick": True}, {}, {"hedge_arrival_book": "view"}, ticks=True, needs_probe=True))
    out.append(r("L4_bybit_only_makers", pm, {"round_quotes_to_tick": True, "maker_venues": ["bybit"]}, {}, {"hedge_arrival_book": "view"}, ticks=True, needs_probe=True))
    out.append(r("L5_hedge_on_arrival_book", pm, {"round_quotes_to_tick": True, "maker_venues": ["bybit"]}, {}, {"hedge_arrival_book": "truth"}, ticks=True, needs_probe=True))
    return out


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    import calendar
    jobs = [(row_["tag"], d) for row_ in rows() for d in DAYS]
    pending, running = list(jobs), {}
    (X / "logs").mkdir(exist_ok=True)
    def done(tag, d): return (RUNS / "ladder9" / tag / d / "summary.json").exists()
    while pending or running:
        for k, p in list(running.items()):
            if p.poll() is not None:
                print(time.strftime("%T"), "rc", p.returncode, *k, flush=True); del running[k]
        i = 0
        while i < len(pending) and len(running) < n:
            tag, d = pending[i]
            row_ = next(x for x in rows() if x["tag"] == tag)
            if done(tag, d):
                pending.pop(i); continue
            if row_["needs_probe"] and not PROBE.exists():
                i += 1; continue
            pending.pop(i)
            t1 = calendar.timegm(time.strptime(d, "%Y-%m-%d")) + 7200 + 1.0
            env = dict(os.environ, QV2_DATA_ROOT=str(X / "slices_mar2024"), QV2_RUNS=str(RUNS), QV2_SNAP=str(X / "snap_qv2i"),
                       QV2_MAKER=json.dumps({"unknown_queue_fit": FIT, **row_["maker"]}),
                       QV2_CFG=json.dumps({"data_manifest": str(X / "manifest_mar2024.json"), "max_event_ts": t1, **row_["cfg"]}),
                       QV2_ARB=json.dumps(row_["arb"]), QV2_MOVES=json.dumps(row_["moves"]), QV2_MOVES_SOURCE=row_["tag"])
            if row_["ticks"]:
                env["QV2_VENUE_TICKS"] = json.dumps(TICKS)
            args = ["nice", "-n", "5", "python3.12", "-I", str(X / "qv2_march_run.py"), "perp_perp_maker_taker_v1_1", d, "--group", "ladder9", "--cell", tag, "--allow-rejected-data", REASON]
            running[(tag, d)] = subprocess.Popen(args, env=env, stdout=open(X / "logs" / f"ladder9_{tag}_{d}.log", "w"), stderr=subprocess.STDOUT)
            print(time.strftime("%T"), "start", tag, d, flush=True)
        time.sleep(5)
    print(time.strftime("%T"), "ladder9 done", flush=True)


if __name__ == "__main__":
    main()
