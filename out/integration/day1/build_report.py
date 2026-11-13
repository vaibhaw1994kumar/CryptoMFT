"""The v1_9 EMS acceptance report (owner 12 Nov: "generate report at last after running the replay"). Reads the replay outputs and the coverage matrix and writes out/integration/v1_9_ems_acceptance_report.html.
A report writer over files: every number is read from a run output, nothing is typed in except the findings text. EXPLORATORY, mechanics only. usage: build_report.py"""
import html
import json
from pathlib import Path

INT = Path("/Users/vaibhav/Documents/Newproject/out/integration")
DAY = INT / "day1"
RUNS = DAY / "runs"
matrix = json.loads((INT / "ems_cases" / "coverage_matrix.json").read_text())


def jl(p):
    return json.loads(Path(p).read_text()) if Path(p).exists() else None


def esc(x):
    return html.escape("" if x is None else str(x))


def fmt(v):
    if isinstance(v, float):
        return f"{v:.4g}"
    return esc(v)


def table(headers, rows, cls=""):
    out = [f"<table class='{cls}'><thead><tr>" + "".join(f"<th>{esc(h)}</th>" for h in headers) + "</tr></thead><tbody>"]
    for r in rows:
        out.append("<tr>" + "".join(f"<td>{fmt(c)}</td>" for c in r) + "</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def paths_section(title, d):
    cov, life = jl(d / "session_ems_path_coverage.json"), jl(d / "session_lifecycle_report.json")
    if cov is None:
        return f"<h3>{esc(title)}</h3><p class='note'>not available: {esc(d.name)} did not produce a coverage file.</p>"
    p = cov["paths"]
    flat = [(k, v) for k, v in p.items() if k != "intent_states"]
    out = [f"<h3>{esc(title)}</h3>", f"<p>Intent states: {esc(json.dumps(p['intent_states']))}. Tactic by order: {esc(json.dumps(cov['tactic_by_order']))}; placement of quotes: {esc(cov['placement'])}; tactic choice: {esc(cov['tactic_choice'])}.</p>",
           f"<p>Per-order checks: off-tick prices {len(cov['checks']['off_tick'])}, caps on the wrong side of the touch {len(cov['checks']['cap_on_the_wrong_side_of_touch'])}, tactic violations {len(cov['tactic_violations'])} &rarr; <b>{'PASS' if cov['checks']['pass'] else 'FAIL'}</b> ({cov['checks']['orders_checked']} orders).</p>",
           "<div class='cols'>" + table(["path", "count"], flat[: (len(flat) + 1) // 2]) + table(["path", "count"], flat[(len(flat) + 1) // 2:]) + "</div>",
           f"<p class='note'>Paths with count 0 were not exercised in this run: {esc(', '.join(k for k in cov['not_exercised'] if k != 'intent_states'))}.</p>"]
    if life:
        t = life["total"]
        q = t["quotes"]
        out.append(f"<h4>Lifecycle, whole run</h4><p>Quotes placed {q['quotes_placed']}, with a fill {q['quotes_filled']}, fill rate {fmt(q['fill_rate']) if q['fill_rate'] is not None else 'n &lt; 30: counts only'}; withdrawn {q['withdrawn']}; "
                   f"races: during cancel pending {q['fills_during_cancel_pending']}, cancel while submitted {q['cancel_while_submitted']}, late after cancel {q['late_fills_after_cancel']}. Matched entry spread {esc(json.dumps(q['matched_entry_spread_bps']))}; "
                   f"lifecycle net P&amp;L {esc(json.dumps(q['lifecycle_net_pnl_usdt']))}.</p>")
        out.append(f"<p>Message load per minute: {esc(json.dumps(t['message_load_per_minute']))}. Residual exposure: {esc(json.dumps(t['residual_exposure']))}. Hedge completion: {esc(json.dumps(t['hedge_completion']))}. Adverse tails (markout bps): {esc(json.dumps(t['adverse_tails_markout_bps']))}.</p>")
        for dim in ("volatility", "spread", "liquidity", "utc_hour"):
            cells = life["by_regime"][dim]
            rows = []
            for k, c in cells.items():
                q = c["quotes"]
                rows.append([k, q["quotes_placed"], q["quotes_filled"], fmt(q["fill_rate"]) if q["fill_rate"] is not None else "n < 30", q["fills_during_cancel_pending"] + q["late_fills_after_cancel"], c["hedge_completion"]["maker_fills"],
                             c["hedge_completion"]["fully_hedged"], fmt(c["residual_exposure"]["oldest_exposure_age_s"])])
            out.append(f"<h4>By {dim.replace('_', ' ')}</h4>" + table(["bucket", "quotes", "quotes filled", "fill rate", "races", "maker fills", "fully hedged", "oldest exposure (s)"], rows))
        out.append(f"<p class='note'>Regime boundaries: {esc(json.dumps(life['regime_boundaries']))}. Partition check passed: {life['partition_ok']}.</p>")
    return "".join(out)


n_pass = sum(1 for m in matrix if m["result"] == "PASS")
n_fail = sum(1 for m in matrix if m["result"] == "FAIL")
n_nc = sum(1 for m in matrix if m["result"] == "NOT COVERED")

rows = [[m["case"], m["covering_layer"], m["count"], m["result"], m["detail"], m["evidence"]] for m in matrix]
matrix_html = table(["Path or case", "Covering layer", "Count", "Result", "Detail", "Evidence"], rows, "matrix")
for word, color in (("PASS", "#1a7f37"), ("FAIL", "#b42318"), ("NOT COVERED", "#8a6a00")):
    matrix_html = matrix_html.replace(f"<td>{word}</td>", f"<td style='font-weight:600;color:{color}'>{word}</td>")

body = f"""
<h1>v1_9 EMS acceptance replays</h1>
<p class='sub'>Report of the owner's order of 12 Nov 2026 against the EMS note of 09 Nov. EXPLORATORY, mechanics only: no profit claim anywhere. 2026-10-29 SOLUSDT capture (both venues), 1 SOL unless a case says otherwise,
fees as in profile v1_9, mark = mid, no funding. Pass and fail rules are those of <code>v1_9_TOLERANCES.md</code>, written and approved before these runs.</p>
<div class='kpis'><div><b>{n_pass}</b><span>checks passed</span></div><div><b>{n_fail}</b><span>failed</span></div><div><b>{n_nc}</b><span>not covered</span></div></div>

<h2>1. What this report is, and what it is not</h2>
<ul>
<li><b>Safety values (owner 13 Nov).</b> Every v1_9 run here uses the PROFILE's own safety values: the layer risk gate in the profile's mandatory <code>enforce</code> mode and the profile's <code>breaker</code> block (<code>profile_safety</code>), not the replay's older defaults (gate in count mode, no breaker). Not set by the profile and therefore unchanged: the layer route mode and the shadow reconciliation mode (count), and the session's pre-send risk gate (off). Earlier numbers of these cases (gate in count, no breaker) are superseded; the attribution runs are in <code>day1/attribution/</code>.</li>
<li>Replays run from frozen code snapshots, one job at a time, fixed seeds. The determinism check (one scenario run twice) is the first row of the matrix.</li>
<li>Regime boundaries (volatility, spread, liquidity terciles) of the 29 Oct runs are <b>in-sample</b>: the day's own terciles (the seven March training days are not on this machine). They describe that day; they are not an out-of-sample result. The March replay uses a PARTIAL training-slice file (2 hours of 03-01).</li>
<li>The spread tercile is nearly degenerate on March (one tick is almost every second), so its split carries little information.</li>
<li>Most cells hold fewer than 30 quotes, so they print counts and no rate.</li>
<li>Scenario windows are 2-hour cuts of the capture. The books are warmed from the capture's start (09:56 UTC) with quoting off until the window opens.</li>
</ul>

<h2>2. Coverage matrix</h2>
{matrix_html}

<h2>3. Clean replays: EMS path coverage, tactic and placement, per-order checks, lifecycle by regime</h2>
{paths_section("2026-10-29, full capture, v1_9, 1 SOL", RUNS / "clean_day1_v19")}
{paths_section("March 2024-03-11, local 2-hour slice, v1_9, 1 SOL (Binance has best bid/ask only)", RUNS / "clean_march_0311_v19")}
<p class='note'>2022-01-05 is not covered: v1_9 pins a hedge-move calibration fitted on March 2024 training days, and the profile loader refuses a run that starts before the calibration's days. A January replay needs its own profile version.</p>

<h2>4. Findings</h2>
<ol>
<li><b>A real defect, found by the 2d replay and fixed.</b> With a 3 s ghost hedge outage a hedge chain moved RETRYING &rarr; HEDGED, which the transition table did not allow, and the replay stopped with IllegalHedgeTransition. A late success of an earlier child can cover the exposure while a retry is still waiting out its backoff. The table now allows RETRYING &rarr; HEDGED and PARTIALLY_FILLED, and the waiting retry timer is dropped when the chain moves on (no stale timer, no second hedge). A regression test pins it.</li>
<li><b>Unwind limit rounding.</b> The unwind IOC limit used to round a buy up and a sell down (looser than its band by up to a tick). It now rounds without loosening, as the hedge cap does.</li>
<li><b>Rate-limit pressure.</b> With the limit lowered to 6 requests per 10 s, hedges are refused above 80% limiter use (the risk-reducing tier). The run still ended flat through an escalation and two unwind orders, but four hedges were refused. Refusals are also followed by new quote attempts (more orders created than in the baseline window), so the lowered limit does not reduce the attempt count. The tier share (80%) is an ASSUMED value and the decision to admit hedges above it is the owner's.</li>
<li><b>Fill during cancel pending</b> occurs naturally with a longer cancel latency (800 ms) and is booked once and hedged once.</li>
<li><b>The size-aware hedge cap never left the touch at 1, 5 or 25 SOL</b>: on this capture the Binance touch covered the hedge size every time (8, 11 and 39 hedges, all filled in full, no partial, no zero, no unwind, the absolute limit never bound). The size-spanning, absolute-limit and fallback paths are therefore exercised by the unit tests only, not by any replay; the IOC partial and zero cases (2c) have no natural occurrence in this capture either.</li>
<li><b>Maker-venue outage, restart inside a replay, fill-record field (new-level vs queued), amend at replay level (no S0 strategy exists), breaker at replay level, and sampled p99 / p99.9 latency are not built</b>; each is listed in the matrix with its reason. The unit-level evidence for restart, amend, breaker alerts and late-success reconciliation exists as stage-trace files in <code>ems_cases/</code>.</li>
<li><b>The profile's enforce gate refused nothing in any of the 22 runs</b> (20 scenarios, the 2024-03-11 slice, the full 29 Oct day): risk_refusals 0 and would-refuse 0 everywhere. Enforce alone changes nothing: the five cases below were rerun with the gate in enforce and the breaker OFF and are identical to their earlier runs (orders, fills, signals, decisions, intent journals).</li>
<li><b>Layer risk gate in the profile's enforce mode: NONE of the orders, fills or gate decisions of any v1_9 case changed because of it</b> (owner 13 Nov, stated explicitly). The 22 runs under the profile's values refused nothing (risk_refusals 0, would-refuse 0), and the five cases that differed from their earlier runs were rerun with the gate in enforce and the breaker OFF: identical to the earlier runs (orders, fills, signals, decisions, intent journals). Every difference below is the breaker.</li>
<li><b>Breaker rules (owner 13 Nov, built after the first profile-values rerun): a net residual below one lot of the hedge venue is untradeable dust.</b> It is still counted (breaker summary, end-of-day report) but does not start the 5 s net-exposure timer; one lot or more held past the limit trips as before. After a trip a flatten remainder below the venue's minimum quantity or notional is DUST: recorded once (BREAKER_FLATTEN_DUST / UNWIND_DUST, with the minimums), never retried, and the trip stays latched until a manual reset; a venue that is unavailable is retried 1 s, 2 s, 4 s ... capped at 30 s, and an unavailable or below-minimum skip is one event per state change plus a counter. <b>The case that produced 168,133 UNWIND_VENUE_SKIPPED events (2c_zero_exhausted, and 2n_size25 with the same number) now produces 0</b>: 2c_zero_exhausted writes 23 safety events in all (8 unwind intents, 2 UNWIND_DUST, 1 BREAKER_FLATTEN_DUST), 2n_size25 none.</li>
<li><b>What the profile's breaker changes now: four cases.</b> 2d_ghost3 (3 consecutive hedge failures, exposure 2.04 s old): signals 5 to 2, end positions 4 SOL to 1 SOL. 2d_ghost30 (net exposure of 1 SOL over 5 s, kill switch): 1 SOL to flat. 2c_zero_then_fill (the breaker counts a chain's FIRST outcome, so three plans whose first IOC found nothing are three failures although every retry filled, as designed on 11 Nov): signals 5 to 4, positions 5 to 3 SOL, and its matrix rule FAILS (3 unwind orders; the breaker-off attribution row passes). 2c_zero_exhausted: the 0.0076 SOL remainder no longer trips it; it now trips after three plans whose hedge failed on every attempt (3 consecutive failures), flattens, and records the leftover as dust: orders 27 to 20, signals 7 to 3, matrix rule FAILS (the breaker stops the plans the rule counts; the breaker-off attribution row passes). 2n_size25 is now identical to its pre-breaker run: the dust remainder no longer ends the day (39 hedges, positions 125 SOL). The other scenario runs are identical; the two clean runs differ only in ledger event ids.</li>
<li><b>One accounting question left for the owner, not changed here.</b> A flatten plan that closes with its remainder as dust still carries that remainder as a residual of its own, on top of the closed plan that first left it: in 2c_zero_exhausted the end-of-day carried residual reads 0.01286 SOL (above one lot) against a real net of -0.00238 SOL. A carried residual at or above the risk bound refuses the next package, so after a manual reset the double count could refuse new risk that is not there.</li>
</ol>

<h2>5. Evidence files</h2>
<p>Stage traces: <code>out/integration/ems_cases/*.jsonl</code> (unit cases <code>case*_</code>, scenario replays <code>scn_*</code>). Replay outputs: <code>out/integration/day1/runs/scn_*</code>, <code>clean_day1_v19</code>, <code>clean_march_0311_v19</code>. Matrix: <code>ems_cases/coverage_matrix.md</code> and <code>.json</code>.
Documents: <code>v1_9_TOLERANCES.md</code> (rules), <code>production_limits_proposal.md</code> (limits, awaiting the owner's approval).</p>
"""

css = """
body{font:15px/1.5 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;margin:0;padding:24px 16px;color:#1f2328;background:#fff}
main{max-width:1100px;margin:0 auto} h1{font-size:26px;margin:0 0 4px} h2{font-size:19px;margin:32px 0 8px;border-bottom:1px solid #d0d7de;padding-bottom:4px} h3{font-size:16px;margin:22px 0 6px} h4{font-size:14px;margin:14px 0 4px}
.sub{color:#57606a;margin-top:0} .note{color:#57606a;font-size:13px} code{background:#f2f4f6;padding:1px 4px;border-radius:4px;font-size:13px}
table{border-collapse:collapse;width:100%;margin:8px 0;font-size:13px} th,td{border:1px solid #d0d7de;padding:4px 8px;text-align:left;vertical-align:top} th{background:#f6f8fa}
.matrix td:nth-child(5){max-width:420px;overflow-wrap:anywhere} .cols{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.kpis{display:flex;gap:12px;margin:16px 0}.kpis div{border:1px solid #d0d7de;border-radius:8px;padding:10px 18px}.kpis b{font-size:26px;display:block}.kpis span{color:#57606a;font-size:13px}
@media (max-width:700px){.cols{grid-template-columns:1fr} table{display:block;overflow-x:auto}}
@media (prefers-color-scheme:dark){body{background:#0d1117;color:#e6edf3} .sub,.note,.kpis span{color:#9da7b3} th{background:#161b22} th,td,h2,.kpis div{border-color:#30363d} code{background:#161b22}}
"""
doc = f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>v1_9 EMS acceptance replays</title><style>{css}</style></head><body><main>{body}</main></body></html>"
(INT / "v1_9_ems_acceptance_report.html").write_text(doc)
print("written", len(doc))
