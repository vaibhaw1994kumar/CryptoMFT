"""v1.1 switch ladder (L3 audit decisions C and D, 2026-11-04). Cumulative, one switch per step, each step a full replay on the frozen snapshot snap_qv2g.
singapore latency, bybit_perp_mm fees, clean preset, size 1, seed 1, 2022-01-05 and 2022-01-13 (EXPLORATORY, 2 partial days). Resumable.
usage: qv2_ladder_pool.py N_PARALLEL [JOBSET ...]   JOBSET: ladder | profile | models | upper   (default all)"""
import json, os, subprocess, sys, time
from pathlib import Path
X = Path("/Users/vaibhav/Documents/Newproject/out/queue_v2")
SNAP = X / "snap_qv2g"
RUNS = X / "runs_ladder"
MAN = str(X / "manifest.json")
REASON = "exploratory: Binance trade tape truncated at ~17 h on 2022-01-05 and 2022-01-13 (queue_v2 manifest); Bybit maker fills use the Bybit tape"
T13 = "2022-01-05,2022-01-06,2022-01-07,2022-01-08,2022-01-09,2022-01-10"
DAYS = ["2022-01-13", "2022-01-05"]                      # the slow day first
FIT = lambda d: str(X / f"unknown_queue_fit_for_{d}.json")


def steps(d):
    """(tag, profile, maker overrides, cfg overrides). Cumulative: each step adds exactly one switch to the previous one."""
    s0 = ("s0_v1_baseline", {}, {})
    s1 = ("s1_manifest_gate", {}, {"data_manifest": MAN})
    s2 = ("s2_dedupe_multiset", {}, {"data_manifest": MAN, "trade_dedupe": "multiset"})
    s3 = ("s3_ledger_epoch", {"ledger_epoch": True}, s2[2])
    s4 = ("s4_own_overlay", s3[1], {**s2[2], "own_impact": "overlay"})
    s5 = ("s5_no_tape_repair", s3[1], {**s4[2], "book_tape_repair": False})
    s6 = ("s6_fitted_unknown_queue", {**s3[1], "unknown_queue_dist": "fitted", "unknown_queue_fit": FIT(d), "unknown_queue_floor": 0.0, "unknown_queue_fallback": 25.0}, s5[2])
    s7 = ("s7_evidence_required", {**s6[1], "vanish_clears_queue": False, "fill_evidence": "trade_confirm"}, s5[2])
    return [(t, "perp_perp_maker_taker_v1", m, c) for t, m, c in (s0, s1, s2, s3, s4, s5, s6, s7)], s7


# Time cut (owner, 2026-11-04): 2022-01-05 gets every step, the D models and the upper bound; 2022-01-13 only the baseline and the final step.
# s1 (manifest gate + truncation count) changes no behaviour by design and is not replayed. Everything dropped is listed in the report.
PLAN = {("2022-01-05", t) for t in ("s0_v1_baseline", "s2_dedupe_multiset", "s3_ledger_epoch", "s4_own_overlay", "s5_no_tape_repair", "s6_fitted_unknown_queue", "s7_evidence_required",
                                    "d_behind_first", "d_ahead_first", "d_cohort_uniform", "ub_depth_only_fills_on")} | {("2022-01-13", t) for t in ("s0_v1_baseline", "s7_evidence_required")}


def jobs(sets):
    return [j for j in _jobs(sets) if (j[1], j[0]) in PLAN or "profile" in sets]


def _jobs(sets):
    out = []
    for d in DAYS:
        lad, s7 = steps(d)
        if "ladder" in sets:
            out += [(t, d, p, m, c) for t, p, m, c in lad]
        if "profile" in sets:        # the profile FILE v1_1 must land where the ladder ended: same settings from the yaml (+ the day's fit path and the manifest, which the runner supplies)
            out.append(("s7_from_profile_file", d, "perp_perp_maker_taker_v1_1", {"unknown_queue_fit": FIT(d)}, {"data_manifest": MAN}))
        if "models" in sets:         # D: v1.1 under the other queue models (proportional = s7)
            for t, mk in (("d_behind_first", {"cancel_model": "behind_first"}), ("d_ahead_first", {"cancel_model": "ahead_first"}),
                          ("d_cohort_uniform", {"queue_model": "cohort", "cohort_age": "uniform", "opposite_cross_fills": False})   # the cohort queue refuses opposite_cross_fills (engine check): this one scenario runs without it, stated in the report
                          ):
                out.append((t, d, "perp_perp_maker_taker_v1", {**s7[1], **mk}, s7[2]))
        if "upper" in sets:          # D: UPPER-BOUND scenario only: v1.1 with the depth-only fills switched back on (never a headline)
            out.append(("ub_depth_only_fills_on", d, "perp_perp_maker_taker_v1", {**s7[1], "vanish_clears_queue": True, "fill_evidence": "confirm_delta"}, s7[2]))
    return out


def main():
    n = int(sys.argv[1]); sets = set(sys.argv[2:]) or {"ladder", "models", "upper"}
    pending, running = jobs(sets), {}
    (X / "logs").mkdir(exist_ok=True)
    def done(tag, prof, day): return (RUNS / prof / f"singapore_bybit_perp_mm_clean_s1_{tag}" / day / "summary.json").exists()
    while pending or running:
        for k, p in list(running.items()):
            if p.poll() is not None:
                print(time.strftime("%T"), "rc", p.returncode, *k, flush=True); del running[k]
        while pending and len(running) < n:
            tag, day, prof, mk, cf = pending.pop(0)
            if done(tag, prof, day): continue
            env = dict(os.environ, EXEC_SNAP=str(SNAP), EXEC_OUT=str(RUNS), EXEC_TAG=tag, QV2_MAKER=json.dumps(mk), QV2_CFG=json.dumps(cf))
            args = ["nice", "-n", "5", "python3.12", str(X / "qv2_run.py"), prof, day, "--allow-rejected-data", REASON]
            if day == "2022-01-13": args += ["--train", T13]
            running[(tag, day)] = subprocess.Popen(args, env=env, stdout=open(X / "logs" / f"ladder_{tag}_{day}.log", "w"), stderr=subprocess.STDOUT)
            print(time.strftime("%T"), "start", tag, day, flush=True)
        time.sleep(5)
    print(time.strftime("%T"), "ladder pool done", sorted(sets), flush=True)


if __name__ == "__main__":
    main()
