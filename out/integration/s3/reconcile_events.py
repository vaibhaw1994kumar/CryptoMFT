"""Reconcile the S3 SELECT events with the v1_6 replay (owner 10 Nov), a run wrapper: for each of the `clear`-rule events of the four SELECT slices (March 2024 03-08..03-11, 00:00-02:00 UTC) say what the replay did at that moment, and which tool is wrong.
Inputs: the event files re-extracted with `entry_at_strategy_view` (study_recon/days: the entry rule re-evaluated on what the strategy could SEE when it had to decide: the maker book 8 ms old, the hedge book 42 ms old; ASSUMED singapore-preset delays) and the v1_6 replay of each slice with
a reject trace (s1/runs_recon/<day>: every reject with the time of its evaluation, the signals, the maker orders and their fills).
CLASSIFICATION per event (T = the sweep's exchange time; the replay's own clock is receive time, 2 ms later on Bybit):
  FILLED_IN_REPLAY            a replay maker order of that side was resting at T and was filled within 100 ms of T
  RESTING_QUEUE_NOT_REACHED   a replay maker order of that side was resting at T and was not filled by this sweep
  SIGNAL_NEAR_NOT_RESTING     the replay signalled within 250 ms before T but the order was not resting at T
  REJECTED:<reason>           no quote of ours: the reason of the last reject the strategy logged before T - 6 ms (within 250 ms), e.g. no_edge, toxic, cross_venue_skew, clip_too_small, maker_limit
  NO_EVALUATION               the replay logged no evaluation in that window (strategy not run: book gate, suspension)
Each is cross-tabulated with the study's own strategy-view verdict (ok / no_edge / ...): where the study's view says the entry rule FAILS and the replay says no_edge, the study (exchange-time books, no feed delay) was optimistic; where the view says it passes and the replay
rejects, the difference is a gate or a feature the study does not model (named by the reason).
usage: reconcile_events.py OUT_JSON"""
import collections
import json
import sys
from pathlib import Path

ROOT = Path("/Users/vaibhav/Documents/Newproject/out/integration")
DAYS = ["2024-03-08", "2024-03-09", "2024-03-10", "2024-03-11"]
PROFILE = "perp_perp_maker_taker_v1_6"


def lines(p):
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


def main():
    out, rows = Path(sys.argv[1]), []
    for day in DAYS:
        run = ROOT / "s1" / "runs_recon" / day / "v16g" / "base" / PROFILE / day
        ev = [r for r in json.loads((ROOT / "s3" / "study_recon" / "days" / f"{day}.json").read_text())["records"] if r["rule"] == "clear"]
        trace = lines(run / "session_reject_trace.jsonl")
        signals = lines(run / "session_signals.jsonl")
        times = json.loads((run / "order_times.json").read_text())
        makers = [t for t in times if t["venue"] == "bybit"]
        orders = {(o["side"].lower(), ) : None for o in lines(run / "session_orders.jsonl")}
        orders_list = [o for o in lines(run / "session_orders.jsonl") if o["venue"] == "BYBIT"]
        tr_ts = [t[0] for t in trace]
        import bisect

        for e in ev:
            T, side = e["ts"], e["side"]
            want = "buy" if side == "bid" else "sell"
            outcome = None
            for m in makers:
                end = max(m["fills"]) if m["fills"] else m["arrival"] + 15.0
                if m["side"] == want and m["arrival"] <= T + 0.002 <= end + 0.001:
                    fill_near = [f for f in m["fills"] if abs(f - T) <= 0.1]
                    outcome = "FILLED_IN_REPLAY" if fill_near else "RESTING_QUEUE_NOT_REACHED"
                    break
            if outcome is None:
                near = [s for s in signals if T - 0.25 <= s["ts"] <= T]
                if near:
                    outcome = "SIGNAL_NEAR_NOT_RESTING"
            if outcome is None:
                lo, hi = bisect.bisect_left(tr_ts, T - 0.25), bisect.bisect_right(tr_ts, T - 0.004)
                win = [t for t in trace[lo:hi] if t[0] is not None]
                outcome = f"REJECTED:{win[-1][1]}" if win else "NO_EVALUATION"
            rows.append({"day": day, "ts": T, "side": side, "replay": outcome, "study_view": e["entry_at_strategy_view"]["reason"] if e["entry_at_strategy_view"]["ok"] is not None else "no_state",
                         "study_toxic_flag": bool(e["toxic_gate_blocks"].get("0.008")), "net_bps": -e["cost_bps_slack5"] if e.get("cost_bps_slack5") is not None else None})
    cross = collections.Counter((r["study_view"], r["replay"]) for r in rows)
    res = {"label": "EXPLORATORY: reconciliation of the S3 SELECT events (clear rule) with the v1_6 replay, March 2024 2-hour slices", "events": len(rows), "by_replay_outcome": dict(collections.Counter(r["replay"] for r in rows)),
           "by_day": {d: dict(collections.Counter(r["replay"] for r in rows if r["day"] == d)) for d in DAYS}, "cross_tab_study_view_x_replay": {f"{a} | {b}": n for (a, b), n in sorted(cross.items())},
           "study_view_ok_share": sum(1 for r in rows if r["study_view"] == "ok") / max(1, len(rows)), "replay_signals": {d: len(lines(ROOT / "s1" / "runs_recon" / d / "v16g" / "base" / PROFILE / d / "session_signals.jsonl")) for d in DAYS}, "rows": rows}
    out.write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps({k: v for k, v in res.items() if k != "rows"}, indent=1, default=str))


main()
