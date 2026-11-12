"""Spot simulator validation, step 4: out/sim_validation_spot/report.html from the committed aggregates (no per-order rows, no order ids).

usage: python3.12 -m new_backtest.validation.build_report --root <outer worktree>/out/sim_validation_spot
Reads steps_1_3/step2_evaluation.json, steps_1_3/step3_fill_model.json, step5_perp/taker_check.json, step5_perp/stats/*.json,
march_tie_check/bybit_SOLUSDT_ties.jsonl and rebuild_acceptance_wt/*/*.json. Step 0's own numbers (T0-T3, T5) are quoted from its report (10 Nov)."""
from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

MODELS = ("proportional", "behind_first", "ahead_first")
TOLERANCES_SHA = "50924c3140e4dd1e37f466b03cc62a7bf6d171c24711af8f0b00478f83711bfb"


def pct(x, d=1):
    return "–" if x is None else f"{100 * x:.{d}f}%"


def ci(r):
    lo, hi = r.get("ci95") or [None, None]
    return "" if lo is None else f" <span class=mut>[{pct(lo)}, {pct(hi)}]</span>"


def kn(r):
    return f"{r['k']}/{r['n']}" + (f" = {pct(r['rate'])}" if r.get("rate") is not None else "")


def badge(v):
    cls = {"PASS": "ok", "IMPROVEMENT": "ok", "FAIL": "bad", "NO IMPROVEMENT": "bad"}.get(v, "warn")
    return f"<span class='badge {cls}'>{html.escape(v)}</span>"


def table(head, rows, cls=""):
    h = "".join(f"<th>{c}</th>" for c in head)
    b = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f"<div class=tw><table class='{cls}'><thead><tr>{h}</tr></thead><tbody>{b}</tbody></table></div>"


def timing_cells(t):
    return [str(t["n"]), pct(t["hit"]["rate"]), pct(t["premature"]["rate"]), pct(t["late"]["rate"]), pct(t["unfilled"]["rate"]), pct(t["sim_rejected"]["rate"])]


def build(root: Path) -> str:
    s2 = json.loads((root / "steps_1_3/step2_evaluation.json").read_text())
    s3 = json.loads((root / "steps_1_3/step3_fill_model.json").read_text())
    s5 = json.loads((root / "step5_perp/taker_check.json").read_text())
    st5 = {p: json.loads((root / f"step5_perp/stats/{p}_2026-04-29.json").read_text()) for p in ("ADAUSDT", "MNTUSDT")}
    ties = [json.loads(l) for l in open(root / "march_tie_check/bybit_SOLUSDT_ties.jsonl")]
    R, V = s2["results"], s2["verdicts"]
    p = R["proportional"]
    t4 = {}
    for f in sorted((root / "rebuild_acceptance_wt").glob("*/spot_*.json")):
        d = json.loads(f.read_text())
        key = "delta_first" if f.stem.endswith("_delta_first") else "legacy"
        t4.setdefault(key, []).append(min(v["touch_price_match_pct"] for v in d["variants"].values()) >= 99.0)
    v3 = s3["verdict"]
    head = s3["folds"][v3["test_day"]]["by_horizon"]

    sec = []
    # summary
    sec.append(f"""<section id=summary><h2>Summary</h2>
<p class=lede>Our real Bybit spot PostOnly orders on ADAUSDT and MNTUSDT, 19 April to 2 May 2026 ({R['proportional']['orders']:,} orders), replayed one by one through the
simulator at their real create and cancel times, and scored against what really happened. Verdicts use the pass rules fixed in <code>TOLERANCES.md</code> on 10 Nov,
before any number existed. <b>EXPLORATORY.</b></p>
{table(["Check", "Pre-registered rule", "Result", "Verdict"], [
        ["2a False fills on real cancels", "point ≤ 5 % and upper bound ≤ 10 %, same under all 3 cancel models",
         " · ".join(f"{m}: {pct(R[m]['cancelled_false_fill']['all']['rate'], 2)}" for m in MODELS), badge(V["2a_false_fill"]["verdict"])],
        ["2b First-fill timing (real fills)", "hit within 5 pts of 80.5 % and premature ≤ 10 %",
         f"hit {pct(V['2b_fill_timing']['hit'])}, premature {pct(V['2b_fill_timing']['premature'])}", badge(V["2b_fill_timing"]["verdict"])],
        ["2c PostOnly reject at arrival", "sensitivity ≥ 70 % and false rejects ≤ 2 %",
         f"sensitivity {kn(V['2c_postonly']['sensitivity'])}, false rejects {kn(V['2c_postonly']['false_reject_rate'])}", badge(V["2c_postonly"]["verdict"])],
        ["3 FillModelV2 on real outcomes", "Brier ≥ 5 % below base rate on 05-02, not worse on ≥ 70 % of days, slope 0.8–1.2",
         f"Brier −{pct(v3['brier_reduction_vs_base_rate'], 0)}, not worse on {v3['folds_not_worse']} days, slope {v3['calibration_slope']:.2f}", badge(v3["verdict"])],
        ["5 Perp taker (8 orders)", "gross-error check only, not a calibration",
         f"{s5['aggregate']['singapore_6ms']['exact_price']}/8 exact price, {s5['aggregate']['singapore_6ms']['qty_equal']}/8 exact quantity", badge("NO GROSS ERROR")],
    ], "summary")}
<ul class=keys>
<li><b>False fills are rare</b>: {kn(p['cancelled_false_fill']['all'])} of real cancels under proportional, all on MNT (ADA 0 of 721). The simulator does not invent fills for orders that were cancelled.</li>
<li><b>Fill timing is consistent with the earlier study</b> (83.9 % vs 80.5 %), but it is an <b>upper bound</b>: the public trade tape contains our own fills, so a really filled order always has its print in the data.</li>
<li><b>PostOnly fails on false rejects</b>: the simulator refuses {pct(V['2c_postonly']['false_reject_rate']['rate'])} of orders the exchange accepted (limit 2 %); it catches {V['2c_postonly']['sensitivity']['k']} of {V['2c_postonly']['sensitivity']['n']} real rejects.
The cause is in the data, not the check: these orders were <b>amended</b>, and the export records only their final price (<a href='#amend'>details</a>); without them the false-reject rate is 0.</li>
<li><b>FillModelV2 is not an improvement</b>: its Brier score beats the base rate, but its probabilities are badly calibrated (slope {v3['calibration_slope']:.2f}); 2 May is a different regime (5 s fill rate {pct(head['5']['observed_rate'])} against {pct(head['5']['base_rate_fit'])} on the fit days).</li>
</ul></section>""")

    # scope
    sec.append(f"""<section id=scope><h2>Scope, data and settings</h2>
{table(["Item", "Value"], [
        ["Market", "Bybit spot, ADAUSDT and MNTUSDT; 2026-04-19 to 05-02 (14 days, 28 coin-days)"],
        ["Books and trades", "public ob200 archive (<code>~/Downloads/bybit_data/spot</code>), about 100 ms resolution, converted in step 0. <b>All 14 days</b>: the recorder capture of 04-19..04-25 was lost (live capture, cannot be downloaded again); owner, 12 Nov: use the archive for those days too"],
        ["Orders", "real PostOnly limit orders from the account export (stays on this Mac; aggregates only here)"],
        ["Engine", "main runner, <code>analysis_mode='maker_only'</code> (same PASSIVE intent, latency, PostOnly and queue path as a strategy order; no strategy, no hedge). Old loop: the only engine path with scheduled orders (deviation from the session-runtime rule, recorded)"],
        ["Profile", "<code>spot_spot_maker_taker_v1</code> (old backtester only, mechanics); latency preset singapore"],
        ["Run-config overrides", "<code>event_order=delta_first</code> (each archive day ends with a 00:00:01 snapshot that already holds its same-timestamp updates; T4 below); <code>trust_local_ts=False</code> (the archive's local stamp is Bybit's send time); <code>maker_max_age_s=0</code> (only real cancels remove an order); spot inventory rule and risk limits off (no strategy)"],
        ["Timing", "released at the real create time + the profile's sampled entry latency; cancelled at exactly the real cancel time (no latency added)"],
        ["Cancel models", "proportional (headline), behind_first and ahead_first (the two bounds)"],
        ["Exclusions", "23 April 06:04–06:20 UTC and orders resting across the resync (ADA 06:08:58.321, MNT 06:09:08.302): none of our orders fell there. 2 orders resting across midnight are censored"],
    ])}</section>""")

    # step 0 + T4
    sec.append(f"""<section id=step0><h2>Step 0: data</h2>
<p>From the step-0 report (10 Nov, worktree at phaseC-gate): T0 raw sha256 48/56 match, 8 without a reference hash, 0 differ; T1 28/28 converted, trade rows equal;
T2 0 sequence gaps; T3 100 % touch match (one snapshot a day). T5 (archive vs recorder) failed on all 14 days and was accepted by the owner as "the archive is coarser".</p>
<p><b>T4 core rebuild check, re-run on this worktree's simulator</b> (11 Nov): with snapshot-first ordering it is identical to step 0 ({sum(t4.get('legacy', []))}/{len(t4.get('legacy', []))} days pass);
with updates first at equal timestamps (<code>delta_first</code>) {sum(t4.get('delta_first', []))}/{len(t4.get('delta_first', []))} pass, and touch size and top 10 levels match on 26 of 28.
The owner accepted the tie explanation on that condition.</p>
<p><b>Re-run on the current core</b> (12 Nov, outer <code>c468a9a</code>, core <code>f7b7cc3</code>, after merging the main branches; cut short at the owner's request):
T4 on 16 of 28 coin-days, 59 of the 84 step-1 replays (4,848 order rows) and step 5 are identical to the earlier runs, field by field. The scores below use the full earlier set.</p></section>""")

    # tolerances
    sec.append(f"""<section id=tol><h2>Tolerances stated before looking</h2>
<p><code>TOLERANCES.md</code>, written 10 Nov 2026 04:37 IST before any converted number or order row was looked at (sha256 <code>{TOLERANCES_SHA[:16]}…</code>, two dated addenda, both about T5 only).
The rules in the summary are applied as written. Stale book = last book message older than 1.0 s at placement; queue buckets in quote currency.</p>
<p class=note><b>Disclosure.</b> The first scoring (12 Nov) used other definitions chosen without re-reading the file: stale = any depth gap over 2 s during the order's life, queue in multiples of the order size.
They were replaced by the pre-registered ones before this report; the earlier breakdowns remain in <code>step2_evaluation.json</code> as supplementary. The pass rules do not use either definition (they shape only the breakdowns), so no verdict depends on the switch.</p></section>""")

    # step 2
    ff_rows = [[m, kn(R[m]["cancelled_false_fill"]["all"]) + ci(R[m]["cancelled_false_fill"]["all"]),
                " · ".join(f"{k}: {v['k']}/{v['n']}" for k, v in R[m]["cancelled_false_fill"]["by_pair"].items()), badge(V["2a_false_fill"]["per_model"][m])] for m in MODELS]
    pa = p["cancelled_false_fill"]
    sec.append(f"""<section id=s2a><h2>Step 2a: false fills on real cancels</h2>
<p>A real CANCELLED order that the simulator fills before the real cancel time. The replay cancels at exactly that time, so any simulated fill is a false fill. Wilson 95 % intervals.</p>
{table(["Cancel model", "False fills", "By coin", "Verdict"], ff_rows)}
<h3>Proportional, by group</h3>
{table(["Group", "False fills"], [[f"day {k}", kn(v)] for k, v in pa['by_day'].items()] +
       [[f"queue ahead (quote) {k}", kn(v)] for k, v in pa['PRE_by_queue_quote'].items()] +
       [[f"book {k}", kn(v)] for k, v in pa['PRE_by_book_age'].items()])}
<p class=mut>839 of the 864 cancels are on 2 May; days with no cancels are not listed.</p></section>""")

    t = p["filled_timing"]
    sec.append(f"""<section id=s2b><h2>Step 2b: first-fill timing on real fills</h2>
<p>Hit = the simulated first fill within 1 s of the real first execution; premature / late = more than 1 s early / late; denominator all real fills (the canonical definition of the cancel-model study).
Reference: 80.5 % hit, proportional, on the fresh-book set of the fill-evidence study (22 coins, collector data, 30 Oct).</p>
{table(["Cancel model", "Orders", "Hit", "Premature", "Late", "Unfilled", "Sim. PostOnly reject"], [[m] + timing_cells(R[m]['filled_timing']['all']) for m in MODELS])}
<h3>Proportional, by group</h3>
{table(["Group", "Orders", "Hit", "Premature", "Late", "Unfilled", "Sim. reject"],
       [[f"coin {k}"] + timing_cells(v) for k, v in t['by_pair'].items()] + [[f"week {k}"] + timing_cells(v) for k, v in t['by_week'].items()] +
       [[f"queue (quote) {k}"] + timing_cells(v) for k, v in t['PRE_by_queue_quote'].items()] + [[f"book {k}"] + timing_cells(v) for k, v in t['PRE_by_book_age'].items()])}
<p class=note>Upper bound: the public tape contains our own real fills, so every really filled order has its fill print in the data; a cancelled order never does. The false-fill rate (2a) is the fairer test.
Orders placed inside the spread (20) are the weakest group: 30 % premature.</p></section>""")

    cm = p["postonly_confusion"]
    sec.append(f"""<section id=s2c><h2>Step 2c: PostOnly reject at arrival</h2>
{table(["", "Simulator rejected", "Simulator accepted"], [
        ["Really rejected", cm.get("real_rejected__sim_rejected", 0), cm.get("real_rejected__sim_accepted", 0)],
        ["Really accepted", cm.get("real_accepted__sim_rejected", 0), cm.get("real_accepted__sim_accepted", 0)]])}
<p>Sensitivity {kn(V['2c_postonly']['sensitivity'])}{ci(V['2c_postonly']['sensitivity'])} (≥ 70 %: met). False-reject rate {kn(V['2c_postonly']['false_reject_rate'])}{ci(V['2c_postonly']['false_reject_rate'])} (≤ 2 %: <b>missed</b>). Identical under all three cancel models (the check runs at arrival, before any queue).
The one PartiallyFilledCanceled order (ADA 29 April) is reported apart.</p></section>""")

    # fill rate including cancels, markouts real vs sim (owner, 12 Nov)
    def bp(x):
        return "–" if x is None or x.get("mean_bps") is None else f"{x['mean_bps']:+.2f} <span class=mut>(n {x['n']})</span>"
    frw, mrw = [], []
    for m in MODELS:
        fm = R[m]["fills_and_markouts"]
        fr = fm["fill_rate"]
        frw.append([m, fr["accepted_orders"], pct(fr["real"]["rate"]), pct(fr["sim"]["rate"]), fr["both_filled"], fr["real_only"], fr["sim_only"], fr["neither"]])
        for h, x in fm["markouts"].items():
            mrw.append([m, h, bp(x["real_fills"]), bp(x["sim_fills"]), bp(x["both_filled_sim_minus_real"]), bp(x["false_fills_sim"])])
    sec.append(f"""<section id=fills><h2>Fill rate including cancels, and markouts real vs simulated</h2>
<p>Over every order the exchange accepted (real cancels included; excluded and censored orders out). "Real only" are orders that really filled and the simulator did not fill:
mostly the simulated PostOnly rejects of amended orders (2c).</p>
{table(["Cancel model", "Accepted orders", "Real fill rate", "Sim. fill rate", "Both filled", "Real only", "Sim. only", "Neither"], frw)}
<p>Maker markout of the first fill, in bps: s × (mid at fill + h − price) / price, s = +1 for a buy; <b>+ means the market moved our way after the fill</b>.
Real fills use the real execution time, simulated fills the simulated fill time; price is the order's limit price; mid is the archive book's.</p>
{table(["Cancel model", "Horizon", "Real fills", "Simulated fills", "Sim − real, orders filled in both", "Simulated false fills"], mrw)}
<p>Under proportional the simulated fills carry the same markout as the real ones to within 0.1 bps at 5 s and 30 s; behind_first fills later and 0.3 bps worse at 30 s.
All markouts here are a fraction of a basis point; the false fills are too few (3–7) to read. The order-by-order table (one row per order and cancel model, schedule index,
no order ids) stays on this Mac in <code>out/sim_validation_spot/local_only/orders_by_order.csv</code> and is not part of this report.</p></section>""")

    # 2c cause and the post-hoc sensitivity
    SN = s2["sensitivity_without_probable_amendments"]
    SR, SV = SN["results"], SN["verdicts"]
    srows = []
    for m in MODELS:
        for lab, blk, vv in (("all orders (pre-registered)", R[m], V), (f"without {SR[m]['probable_amendments_removed']} probable amendments", SR[m], SV)):
            t_ = blk["filled_timing"]["all"]
            srows.append([m, lab, kn(blk["cancelled_false_fill"]["all"]), f"{pct(t_['hit']['rate'])} / {pct(t_['premature']['rate'])}",
                          pct(t_["sim_rejected"]["rate"]), kn(vv["2c_postonly"]["false_reject_rate"]) if m == "proportional" else "same"])
    sec.append(f"""<section id=amend><h2>Why 2c fails: amended orders</h2>
<p>All 52 false rejects cross the opposite touch of the archive book at their real create time (23 exactly at it, the rest 1–11 ticks through), and stay through from 1 s before to 1 s after:
not a timing or latency effect. The book is sound there (never crossed, 1-tick spread on 49 of 52) and the same book flags 37 of the 38 real rejects. Yet Bybit accepted them, and 50 filled
later as makers at exactly the recorded price (median 50 s after creation), 40 of them sitting at their own touch at the fill time.</p>
<p><b>Cause: amendments.</b> Our MM amends live quotes (<code>FAST_MM/bybit_order_manager.py</code> <code>amend_order</code>, <code>/v5/order/amend</code>), and Bybit's order history keeps the
final price with the original create time. The replay therefore places an amended order at its final price at its creation, where it can cross. The account export has no amend flag or
amend history (no such field; the bot logs only failed amends, with truncated ids and no times), so the real price path cannot be replayed.</p>
<p class=note><b>Post hoc sensitivity, not the verdict.</b> Probable amendment = an order Bybit accepted whose recorded price crosses the archive book at its create time (the fill-evidence study's rule (e)).
Amended orders that never crossed cannot be detected and stay in, with a wrong price before their amendment.
Without them: 2a {SV['2a_false_fill']['verdict']}, 2b {SV['2b_fill_timing']['verdict']} (hit {pct(SV['2b_fill_timing']['hit'])}, above the 80.5 % ± 5 band: the rule is two-sided, and the upper-bound caveat applies),
2c {SV['2c_postonly']['verdict']} (false rejects {kn(SV['2c_postonly']['false_reject_rate'])}).</p>
{table(["Cancel model", "Orders", "2a false fills", "2b hit / premature", "2b sim. rejects", "2c false rejects"], srows)}</section>""")

    # step 3
    frows = []
    for d, f in s3["folds"].items():
        x = f["by_horizon"]["5"]
        sl = x.get("calibration_slope")
        frows.append([d[5:], f["n_fit_orders"], x.get("n_uncensored"), f"{x['brier']:.4f}", f"{x['baseline_brier']:.4f}", f"{x['brier_model_minus_baseline']:+.4f}",
                      "–" if sl is None else f"{sl:.2f}"])
    hrows = [[f"{h} s", pct(x["base_rate_fit"]), pct(x["observed_rate"]), f"{x['brier']:.4f}", f"{x['baseline_brier']:.4f}",
              f"{x['brier_model_minus_baseline']:+.4f} <span class=mut>[{x['brier_model_minus_baseline_ci95'][0]:+.4f}, {x['brier_model_minus_baseline_ci95'][1]:+.4f}]</span>",
              "–" if x.get("calibration_slope") is None else f"{x['calibration_slope']:.2f}"] for h, x in head.items()]
    sec.append(f"""<section id=s3><h2>Step 3: FillModelV2 on real outcomes <span class=tag>MEASURED_SPOT_2026</span></h2>
<p>Features: the engine's placement features (the replayed book at the real create time). Labels and censoring: the <b>real</b> outcomes only, never the simulator's fills.
IPCW weights as in <code>fill_model_v1</code>. Chronological folds, each fitted on all earlier days. Not approved as a quote filter.</p>
<h3>Headline: fit 19 April – 1 May, test 2 May</h3>
{table(["Horizon", "Base rate (fit days)", "Observed on 2 May", "Brier model", "Brier base rate", "Difference [95 %]", "Slope"], hrows)}
<h3>All folds, 5 s horizon</h3>
{table(["Test day", "Fit orders", "Test rows (uncensored)", "Brier model", "Brier base rate", "Difference", "Calibration slope"], frows)}
<p>Verdict {badge(v3['verdict'])}: the Brier reduction (−{pct(v3['brier_reduction_vs_base_rate'], 0)}) and the share of days not worse ({v3['folds_not_worse']}) pass; the calibration slope ({v3['calibration_slope']:.2f}) does not.
2 May is flagged as a different quoting regime: its 5 s fill rate is {pct(head['5']['observed_rate'])}, against {pct(head['5']['base_rate_fit'])} on the fit days.
The informative-censoring check could not run: the engine's <code>price_moved_through</code> is false on every row, so the IPCW assumption is untested here.</p>
<p><b>Why it fails.</b> (1) Level: on 2 May the model predicts a mean 5 s fill probability of about 17 % where 0.7 % happened, because the fit days filled at 20 % (regime shift, most 2 May orders were cancelled within seconds: 640 of 895 censored at 5 s).
(2) The slope itself rests on about 2 real fills among the 255 uncensored test rows, so it is not measurable with any precision; both fills sit in the two highest-probability bins, so the ordering is not wrong.
(3) The other folds are unstable too (slope −0.05 to 8.2, mostly on 9 to 100 test rows); on 20 April, with 89 fills, the slope is 0.40, i.e. the probabilities are too spread out.
A pass would need a test period in the same regime as the fit days and many more fills.</p></section>""")

    # step 5
    a5 = s5["aggregate"]
    sec.append(f"""<section id=s5><h2>Step 5: perp taker check <span class=tag>8 orders: gross-error check only, not a calibration</span></h2>
<p>Bybit linear perp, 29 April 2026, converted with <code>bybit_perp_archive.py</code>: ADA {st5['ADAUSDT']['book']['messages']:,} and MNT {st5['MNTUSDT']['book']['messages']:,} book messages, 24.0 h each,
0 sequence gaps, converter rebuild 100 %; core T4 passes under both orders (100 % on all metrics with <code>delta_first</code>).
Raw sha256: ADA book <code>{st5['ADAUSDT']['raw']['orderbook']['sha256'][:12]}…</code>, MNT book <code>{st5['MNTUSDT']['raw']['orderbook']['sha256'][:12]}…</code>.</p>
{table(["Arrival", "Orders", "Exact price", "Exact quantity", "Mean VWAP diff", "Max |diff|"], [
        [k.replace("_", " "), v["orders"], v["exact_price"], v["qty_equal"], f"{v['vwap_diff_bps_mean']:.2f} bps", f"{v['vwap_diff_bps_max_abs']:.2f} bps"] for k, v in a5.items()])}
<p>Our 8 real Market IOC orders (6 ADA, 2 MNT) walked through the core <code>TakerExecutor</code> on the rebuilt book at the real create time + 6 ms (singapore entry latency) match the real executions exactly.
That shows the book's best price is right at each arrival, and nothing more: every order was 66–100 units against hundreds to hundreds of thousands at the touch with a 1-tick spread, so no depth walk, slippage or latency effect is tested.</p></section>""")

    # item 3
    tr = []
    for d in ties:
        for q in d["per_snapshot"]:
            if q["tied_delta_rows"]:
                tr.append([d["day"], q["where"], q["tied_delta_rows"], ", ".join(f"{k} {v}" for k, v in q["event_types"].items()), q["rows_already_in_snapshot"], q["rows_within_top10"]])
    sec.append(f"""<section id=ties><h2>Side check: snapshot/update ties in the March 2024 arb data</h2>
<p>Bybit SOLUSDT, 1–15 March 2024 (read only). Only these snapshots have update rows at the same exchange timestamp; the other {15 - len({r[0] for r in tr})} days have none.</p>
{table(["Day", "Snapshot", "Tied rows", "Types", "Already in the snapshot", "In the top 10"], tr)}
<p>Every tied row is an absolute update or a delete, so the arb's replay (snapshot first) ends with the right book after every tie. <code>delta_first</code> would be wrong on 1 March.
No arb profile was changed; <code>delta_first</code> is used for the spot archive validation only. The live-session recorder replays in arrival order, so the rule does not apply to it.</p></section>""")

    sec.append(f"""<section id=not><h2>What this does not establish</h2><ul>
<li>Spot, not perp; the perp check is 8 small orders.</li><li>Two coins.</li>
<li>One heavy cancel day: 839 of 864 cancels are on 2 May, which is also a different quoting regime.</li>
<li>One book source: the recorder capture of 19–25 April was lost, so there is no second, finer view of the book (the archive updates about every 100 ms; the recorder every 42 ms).</li>
<li>Fill timing is an upper bound (our own fills are in the tape).</li>
<li>The archive has no receive time of ours; the feed delay is the profile's assumption.</li>
<li>The cancel-model bounds are bounds, not measurements.</li>
<li>Exclusion window (owner, latest): orders placed 23 April 06:00–06:20 UTC and orders resting across the resync; none of ours fell there.</li></ul></section>""")

    sec.append("""<section id=repro><h2>Reproduce</h2>
<p>Worktree <code>wt_sim-validation</code>, outer branch <code>sim-validation</code>, core branch <code>sim-validation-core</code>; code in <code>src/new_backtest/validation/</code>
(<code>spot_order_replay</code>, <code>evaluate</code>, <code>fill_model_fit</code>, <code>perp_taker_check</code>, this builder). Per-order rows stay on this Mac in the session scratchpad and carry no order ids.</p></section>""")

    nav = "".join(f"<a href='#{i}'>{n}</a>" for i, n in (("summary", "Summary"), ("scope", "Scope"), ("step0", "Step 0"), ("tol", "Tolerances"), ("s2a", "2a"), ("s2b", "2b"),
                                                      ("s2c", "2c"), ("fills", "Fill rate"), ("amend", "Amendments"), ("s3", "Step 3"), ("s5", "Step 5"), ("ties", "March ties"), ("not", "Limits")))
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Spot Simulator Validation</title><style>
:root{{--bg:#ffffff;--fg:#1c2330;--mut:#5b6677;--line:#d9dee7;--head:#f1f4f9;--acc:#1f5fbf;--ok:#1b7a3a;--bad:#b3261e;--warn:#9a6700;--code:#f4f6fa}}
@media (prefers-color-scheme: dark){{:root:not([data-theme="light"]){{--bg:#10151d;--fg:#e4e9f1;--mut:#9aa6b8;--line:#2a3445;--head:#182030;--acc:#7fb0ff;--ok:#5cc781;--bad:#ff8a80;--warn:#e3b341;--code:#182030}}}}
:root[data-theme="dark"]{{--bg:#10151d;--fg:#e4e9f1;--mut:#9aa6b8;--line:#2a3445;--head:#182030;--acc:#7fb0ff;--ok:#5cc781;--bad:#ff8a80;--warn:#e3b341;--code:#182030}}
body{{background:var(--bg);color:var(--fg);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;margin:0}}
main{{max-width:1040px;margin:0 auto;padding:24px 16px 64px}}h1{{font-size:26px;margin:0 0 4px}}h2{{font-size:20px;margin:36px 0 10px;padding-top:8px;border-top:1px solid var(--line)}}
h3{{font-size:16px;margin:20px 0 8px}}.sub{{color:var(--mut);margin:0 0 16px}}nav{{position:sticky;top:0;background:var(--bg);padding:8px 0;border-bottom:1px solid var(--line);z-index:1;overflow-x:auto;white-space:nowrap}}
nav a{{margin-right:14px;color:var(--acc);text-decoration:none;font-size:14px}}.tw{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;font-size:14px;margin:8px 0}}
th,td{{border:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}}th{{background:var(--head)}}code{{background:var(--code);padding:1px 4px;border-radius:3px;font-size:13px}}
.mut{{color:var(--mut);font-size:13px}}.badge{{font-weight:600;padding:1px 8px;border-radius:10px;border:1px solid currentColor;white-space:nowrap}}.ok{{color:var(--ok)}}.bad{{color:var(--bad)}}.warn{{color:var(--warn)}}
.tag{{font-size:12px;font-weight:600;color:var(--warn);border:1px solid currentColor;border-radius:4px;padding:1px 6px;margin-left:6px;vertical-align:middle}}
.note{{border-left:3px solid var(--warn);padding:6px 10px;background:var(--head)}}.lede{{font-size:16px}}.keys li{{margin:4px 0}}
</style></head><body><main><h1>Spot simulator validation</h1><p class=sub>Bybit spot ADAUSDT / MNTUSDT, 19 April – 2 May 2026, our real PostOnly orders · EXPLORATORY · Track C, 12 Nov 2026</p>
<nav>{nav}</nav>{''.join(sec)}</main></body></html>"""


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    a = ap.parse_args()
    root = Path(a.root)
    (root / "report.html").write_text(build(root), encoding="utf-8")
    print(root / "report.html")
