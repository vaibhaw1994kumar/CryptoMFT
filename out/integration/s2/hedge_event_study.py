"""S2 hedge event study (owner 9 Nov): a run wrapper. Replaces the P&L sweep of C63 (5 trades per cell) by an event study on MARKET DATA.
Events: every sweep on Bybit (the S1 label events) where the v1_6 entry rule would have posted a quote and the sweep would have filled it (`new_backtest.market_hedge_move.HedgeStudy`: the strategy's own `_quote_limit`, tick rounding, join-the-touch and
edge check, the frozen S1 table at 50 ms; an improving quote needs the sweep to print our size, a quote joining the touch needs the sweep to print the displayed touch size plus ours; one counted event per side per second).
For each event and each cap the Binance hedge is played alone: IOC at the touch + slack (touch_slack floor 2, 3, 5 = today, 8, 12 bps, effective slack max(max_walk_bps = 2, floor), re-taken from the touch at each dispatch) or at the break-even-plus-buffer limit of the old
backtester (buffer 0, 1, 2, 4, 8 bps, ONE fixed limit for the chain), with the profile's retry rule (2 retries, 0.25 s backoff), judged at the arrival touch (Binance best bid/ask only: the hedge size is assumed to fit at the touch, and the share of events whose touch
size was below our size is reported). A chain with no fill is unwound on Bybit, banded at 15 bps (ASSUMED, DECISION_PENDING_CALIBRATION in the profile). Delays: dispatch-to-arrival 45 ms and report 45 ms (ASSUMED, no measured Binance delay exists).
Recorded per event and cap: fill on the first attempt, number of submits, time to flat, cost after fees (bps of the quote price, positive = a loss) including the price drift during retries and, for unwound chains, the unwind.
RULES, WRITTEN BEFORE SELECT IS SCORED (owner 9 Nov):
  (1) TRAIN 2024-03-01..03-07 chooses, SELECT 2024-03-08..03-11 confirms; HOLDOUT is never read (this wrapper refuses any day after 03-11). A split with fewer than 30 eligible events is flagged TOO_FEW_TO_COMPARE and nothing is chosen from it.
  (2) The TRAIN choice is the variant with the lowest MEAN cost after fees over ALL events (unwound chains included, so a tight cap cannot look cheap by dropping its failures). Variants within 0.02 bps of the best tie; a tie goes to the lower unwind rate, then to the
      lower 99th-percentile cost. The 95th and 99th percentile costs are reported for every variant.
  (3) SELECT confirms the choice when its SELECT mean cost is no higher than today's cap (slack5) on SELECT by more than 0.02 bps AND its 99th-percentile cost is no more than 2 bps above slack5's on SELECT. If the TRAIN winner is not confirmed, today's cap (slack5) stays.
  (4) If a break-even cell is the TRAIN winner and is confirmed on SELECT: a core change request is written to port it to the session runtime. If not: the break-even cap is recorded as DROPPED.
usage: hedge_event_study.py run [N_PARALLEL]     extract the events of the 11 days (resumable) into out/integration/s2/study/days/
       hedge_event_study.py report                aggregate, apply the rules, write out/integration/s2/study/s2_hedge_event_study.json"""
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path("/Users/vaibhav/Documents/Newproject")
OUT = ROOT / "out" / "integration" / "s2" / "study"
TRAIN = [f"2024-03-0{d}" for d in range(1, 8)]
SELECT = ["2024-03-08", "2024-03-09", "2024-03-10", "2024-03-11"]
VARIANTS = ("slack2", "slack3", "slack5", "slack8", "slack12", "be0", "be1", "be2", "be4", "be8")
MIN_EVENTS = 30
TIE_BPS = 0.02
P99_TOLERANCE_BPS = 2.0


def pct(v, q):
    v = sorted(v)
    return v[min(len(v) - 1, int(len(v) * q))]


def run(n):
    (OUT / "days").mkdir(parents=True, exist_ok=True)
    pending, running = [d for d in TRAIN + SELECT if not (OUT / "days" / f"{d}.json").exists()], {}
    env = {"PYTHONPATH": str(ROOT / "crypto_MFT_arb_platform" / "src"), "PATH": "/usr/bin:/bin:/usr/local/bin:/Library/Frameworks/Python.framework/Versions/3.12/bin"}
    while pending or running:
        for d, p in list(running.items()):
            if p.poll() is not None:
                print(time.strftime("%T"), "rc", p.returncode, d, flush=True)
                if p.returncode == 0:
                    (OUT / "days" / f"{d}.json.part").rename(OUT / "days" / f"{d}.json")
                del running[d]
        while pending and len(running) < n:
            d = pending.pop(0)
            assert d <= "2024-03-11", "HOLDOUT is not read"
            print(time.strftime("%T"), "start", d, flush=True)
            running[d] = subprocess.Popen(["nice", "-n", "5", sys.executable, "-m", "new_backtest.market_hedge_move", "--data-root", str(ROOT / "data"), "--pair", "SOLUSDT", "--days", d, "--out", "/dev/null",
                                           "--hedge-study-out", str(OUT / "days" / f"{d}.json.part")], env=env, cwd=ROOT / "crypto_MFT_arb_platform", stdout=open(OUT / "days" / f"{d}.log", "w"), stderr=subprocess.STDOUT)
        time.sleep(5)
    print("done")


def stats(events, v):
    xs = [e["variants"][v] for e in events]
    cost = [x["cost_bps"] for x in xs]
    flat = [x["time_to_flat_s"] for x in xs if x["outcome"] == "hedged"]
    return {"n": len(xs), "mean_cost_bps": statistics.mean(cost), "p95_cost_bps": pct(cost, 0.95), "p99_cost_bps": pct(cost, 0.99), "worst_cost_bps": max(cost),
            "first_attempt_fill_rate": sum(x["first_attempt_fill"] for x in xs) / len(xs), "mean_submits": statistics.mean(x["submits"] for x in xs),
            "unwind_rate": sum(x["outcome"] != "hedged" for x in xs) / len(xs), "unwind_band_miss": sum(x["outcome"] == "unwound_band_miss" for x in xs),
            "mean_time_to_flat_s_hedged": statistics.mean(flat) if flat else None, "p99_time_to_flat_s_hedged": pct(flat, 0.99) if flat else None,
            "mean_price_drift_bps": statistics.mean(x["price_drift_bps"] for x in xs), "touch_size_below_our_size_at_fill": sum(x["touch_small"] for x in xs) / len(xs)}


def choose(table):
    best = min(r["mean_cost_bps"] for r in table.values())
    tied = [v for v, r in table.items() if r["mean_cost_bps"] <= best + TIE_BPS]
    return min(tied, key=lambda v: (table[v]["unwind_rate"], table[v]["p99_cost_bps"], VARIANTS.index(v)))


def report():
    per_day = {d: json.loads((OUT / "days" / f"{d}.json").read_text()) for d in TRAIN + SELECT}
    res = {"label": "EXPLORATORY: S2 hedge event study, March 2024 market data (Bybit prints, Binance best bid/ask), v1_6 entry rule and frozen S1 table, ASSUMED delays 45 ms, unwind band 15 bps ASSUMED; no simulator fill, no HOLDOUT",
           "rules": __doc__.split("RULES, WRITTEN BEFORE SELECT IS SCORED (owner 9 Nov):")[1].split("usage:")[0].strip(), "assumptions": per_day[TRAIN[0]]["assumptions"], "days": {}, "splits": {}}
    for d, x in per_day.items():
        res["days"][d] = {**{k: v for k, v in x["counts"].items() if k != "label_counts"}, "events": len(x["events"]), "entry_rule_reject_reasons": x["entry_rule_reject_reasons"]}
    for name, days in (("TRAIN", TRAIN), ("SELECT", SELECT)):
        events = [e for d in days for e in per_day[d]["events"]]
        total = {k: sum(per_day[d]["counts"][k] for d in days) for k in ("labelled_events", "entry_rule_rejects", "thinned_by_min_gap", "quote_not_reached_by_sweep_volume", "eligible", "eligible_improving_quote", "touch_size_below_hedge_size_at_decision")}
        sp = {"days": days, "counts": total, "eligible_events": len(events), "flag": "ok" if len(events) >= MIN_EVENTS else "TOO_FEW_TO_COMPARE"}
        if events:
            sp["variants"] = {v: stats(events, v) for v in VARIANTS}
            for v in VARIANTS:       # paired against today's cap on the same events: mean difference in cost (negative = cheaper than slack5) and its standard error
                dif = [e["variants"][v]["cost_bps"] - e["variants"]["slack5"]["cost_bps"] for e in events]
                m = statistics.mean(dif)
                sp["variants"][v]["paired_vs_slack5_mean_bps"] = m
                sp["variants"][v]["paired_vs_slack5_se_bps"] = statistics.pstdev(dif) / len(dif) ** 0.5
            sp["share_touch_size_below_our_size_at_decision"] = total["touch_size_below_hedge_size_at_decision"] / max(total["eligible"], 1)
            sp["by_side"] = {s: sum(1 for e in events if e["side"] == s) for s in ("bid", "ask")}
        res["splits"][name] = sp
    tr, se = res["splits"]["TRAIN"], res["splits"]["SELECT"]
    if tr["flag"] == "ok":
        win = choose(tr["variants"])
        decision = {"train_winner": win, "train_mean_cost_bps": tr["variants"][win]["mean_cost_bps"], "train_today_slack5_mean_cost_bps": tr["variants"]["slack5"]["mean_cost_bps"]}
        if se["flag"] == "ok":
            w, t = se["variants"][win], se["variants"]["slack5"]
            ok = w["mean_cost_bps"] <= t["mean_cost_bps"] + TIE_BPS and w["p99_cost_bps"] <= t["p99_cost_bps"] + P99_TOLERANCE_BPS
            decision.update({"select_winner_mean_cost_bps": w["mean_cost_bps"], "select_slack5_mean_cost_bps": t["mean_cost_bps"], "select_winner_p99_bps": w["p99_cost_bps"], "select_slack5_p99_bps": t["p99_cost_bps"],
                             "confirmed_on_select": bool(ok), "chosen": win if ok else "slack5"})
            if win.startswith("be"):
                decision["break_even"] = "WON_AND_CONFIRMED: write the core change request to port it to the session runtime" if ok else "DROPPED: the break-even cap won TRAIN but SELECT did not confirm it"
            else:
                decision["break_even"] = "DROPPED: a touch-slack cap won TRAIN, not a break-even cap"
        else:
            decision.update({"confirmed_on_select": None, "chosen": "slack5", "note": "SELECT has too few events: nothing confirmed, today's cap stays"})
        res["decision"] = decision
    else:
        res["decision"] = {"chosen": "slack5", "note": "TRAIN has too few events: nothing chosen"}
    (OUT / "s2_hedge_event_study.json").write_text(json.dumps(res, indent=1))
    for name in ("TRAIN", "SELECT"):
        sp = res["splits"][name]
        print(f"--- {name}: {sp['eligible_events']} eligible events ({sp['flag']}); counts {sp['counts']}")
        for v, r in sp.get("variants", {}).items():
            print(f"  {v:8s} first {r['first_attempt_fill_rate']:.3f} submits {r['mean_submits']:.3f} unwound {r['unwind_rate']:.4f} t_flat {r['mean_time_to_flat_s_hedged']:.3f}s  cost mean {r['mean_cost_bps']:+.3f} p95 {r['p95_cost_bps']:+.3f} p99 {r['p99_cost_bps']:+.3f} worst {r['worst_cost_bps']:+.2f}  vs slack5 {r['paired_vs_slack5_mean_bps']:+.3f} +/- {r['paired_vs_slack5_se_bps']:.3f}")
    print(json.dumps(res["decision"], indent=1))


if __name__ == "__main__":
    if sys.argv[1] == "run":
        run(int(sys.argv[2]) if len(sys.argv) > 2 else 2)
    else:
        report()
