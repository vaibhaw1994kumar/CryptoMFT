"""Builds /Users/vaibhav/Documents/Newproject/v1_1_switch_ladder_report.html from out/queue_v2/runs_ladder (nothing typed in by hand except the prose).
Every P&L is EXPLORATORY (2 partial days). usage: python3.12 build_ladder_report.py"""
import html, json, glob, os, math
from pathlib import Path

X = Path("/Users/vaibhav/Documents/Newproject/out/queue_v2")
R = X / "runs_ladder" / "perp_perp_maker_taker_v1"
OUT = Path("/Users/vaibhav/Documents/Newproject/v1_1_switch_ladder_report.html")
esc = html.escape

STEPS = [
    ("s0_v1_baseline", "0", "v1 baseline (frozen reference)"),
    ("s2_dedupe_multiset", "2", "+ trade_dedupe = multiset"),
    ("s3_ledger_epoch", "3", "+ ledger_epoch = true"),
    ("s4_own_overlay", "4", "+ own_impact = overlay"),
    ("s5_no_tape_repair", "5", "+ book_tape_repair = false"),
    ("s6_fitted_unknown_queue", "6", "+ fitted unknown queue"),
    ("s7_evidence_required", "7", "+ evidence-required fills  = v1.1"),
]


def load(tag, day):
    f = glob.glob(str(R / f"*_{tag}" / day / "summary.json"))
    return json.loads(Path(f[0]).read_text()) if f else None


def m(s):
    q = s["qv2"]
    mo = q["markout_summary"]
    c = q["maker_observations"]["maker_fill_causes"]
    d = q["maker_diagnostics"]
    return {
        "fills": s["fills"], "hedges": int(s["counters"].get("hedges_filled", 0)),
        "c_trade": c.get("trade", 0), "c_vanish": c.get("delta_vanish", 0), "c_snap": c.get("snapshot", 0), "c_cross": c.get("opposite_cross", 0),
        "cash": s["cash_pnl"], "eq": s["ending_equity_common_timestamp"],
        "mk1": mo["1s"]["qty_weighted_markout_bps"], "mk5": mo["5s"]["qty_weighted_markout_bps"], "mk30": mo["30s"]["qty_weighted_markout_bps"],
        "mkn": int(mo["1s"]["count"]), "unh": s["exposure"]["time_net_nonzero_s"], "brk": len(s["breaker_trips"]),
        "trunc": s["data_integrity"]["truncated_files"], "fitmiss": d.get("unknown_queue_fit_missing_venue_used_floor", 0),
        "amb": s["qv2"]["maker_observations"].get("maker_fill_uncertainty_flags", {}).get("ambiguous_trade_depth", 0),
        "sent": s["orders_sent"],
    }


def f(x, d=3):
    return "–" if x is None else (f"{x:.{d}f}" if isinstance(x, float) else esc(str(x)))


def dl(x, prev, d=3):
    if prev is None:
        return f(x, d)
    diff = x - prev
    if isinstance(x, float) or isinstance(prev, float):
        return f"{x:.{d}f} <small>({diff:+.{d}f})</small>"
    return f"{x} <small>({diff:+d})</small>"


COLS = [("fills", "maker fills", 0), ("c_trade", "by trade", 0), ("c_vanish", "by delta-vanish", 0), ("c_snap", "by snapshot", 0), ("c_cross", "by opposite-cross (book only)", 0),
        ("hedges", "hedges filled", 0), ("cash", "cash P&amp;L (USDT)", 4), ("eq", "equity, common timestamp", 4), ("mk1", "markout 1 s (bps)", 2), ("mk5", "5 s", 2), ("mk30", "30 s", 2),
        ("unh", "time net-unhedged (s)", 2), ("brk", "breaker trips", 0), ("trunc", "truncated files", 0), ("fitmiss", "fit-missing fallbacks", 0)]


def table(rows, cols=COLS, first="step"):
    h = "<table><thead><tr><th>" + first + "</th>" + "".join(f"<th>{n}</th>" for _, n, _ in cols) + "</tr></thead><tbody>"
    prev = None
    for lab, v in rows:
        h += f"<tr><td class=l>{lab}</td>"
        for k, _, d in cols:
            x = v[k]
            x = float(x) if d else int(x)
            h += "<td>" + dl(x, None if prev is None else (float(prev[k]) if d else int(prev[k])), d) + "</td>"
        h += "</tr>"
        prev = v
    return h + "</tbody></table>"


def plain_table(rows, cols=COLS, first="scenario"):
    h = "<table><thead><tr><th>" + first + "</th>" + "".join(f"<th>{n}</th>" for _, n, _ in cols) + "</tr></thead><tbody>"
    for lab, v in rows:
        h += f"<tr><td class=l>{lab}</td>" + "".join(f"<td>{f(float(v[k]) if d else int(v[k]), d)}</td>" for k, _, d in cols) + "</tr>"
    return h + "</tbody></table>"


s05 = {t: m(load(t, "2022-01-05")) for t, _, _ in STEPS}
s13 = {t: m(load(t, "2022-01-13")) for t in ("s0_v1_baseline", "s7_evidence_required")}
dm = {t: m(load(t, "2022-01-05")) for t in ("d_behind_first", "d_ahead_first", "d_cohort_uniform", "ub_depth_only_fills_on")}

sa = load("s7_evidence_required", "2022-01-05")
acc = {}
for v in ("bybit", "binance"):
    for d in ("2022-01-05", "2022-01-13"):
        j = json.loads((X / "rebuild_acceptance" / f"{v}_{d}.json").read_text())["variants"]
        acc[(v, d)] = (j["engine_tape_repair_on"]["touch_price_match_pct"], j["engine_tape_repair_off"]["touch_price_match_pct"])

ladder_rows = [(f"{n}&nbsp;&nbsp;{esc(lab)}", s05[t]) for t, n, lab in STEPS]
d13_rows = [("0&nbsp;&nbsp;v1 baseline", s13["s0_v1_baseline"]), ("7&nbsp;&nbsp;v1.1", s13["s7_evidence_required"])]
d_rows = [("v1.1 · proportional (calibrated)", s05["s7_evidence_required"]), ("v1.1 · behind_first", dm["d_behind_first"]), ("v1.1 · ahead_first", dm["d_ahead_first"]),
          ("v1.1 · cohort_uniform <sup>*</sup>", dm["d_cohort_uniform"])]
ub_rows = [("UPPER BOUND ONLY: v1.1 + vanish fills on", dm["ub_depth_only_fills_on"])]

css = """body{font:15px/1.5 -apple-system,Segoe UI,sans-serif;max-width:1180px;margin:2rem auto;padding:0 1rem;color:#1b1f24}
h1{font-size:1.5rem}h2{font-size:1.15rem;margin-top:2rem;border-bottom:1px solid #ddd;padding-bottom:.2rem}
table{border-collapse:collapse;font-size:12.5px;margin:.6rem 0 1rem;display:block;overflow-x:auto}th,td{border:1px solid #d5d9de;padding:3px 7px;text-align:right;white-space:nowrap}
th{background:#f2f4f7}td.l,th:first-child{text-align:left}small{color:#6b7280}
.tag{display:inline-block;background:#fff3cd;border:1px solid #e6c200;border-radius:4px;padding:1px 8px;font-size:12px;font-weight:600}
.note{background:#f6f8fa;border-left:4px solid #8a94a3;padding:.5rem .8rem;margin:.8rem 0}.bad{border-left-color:#c0392b}
code{background:#f1f3f5;padding:0 3px}@media(prefers-color-scheme:dark){body{background:#14171b;color:#e6e8eb}th{background:#222831}td,th{border-color:#363d47}.note{background:#1d2229}code{background:#232a33}.tag{background:#4a3f10;color:#fff}}"""

b = s05["s0_v1_baseline"]; e = s05["s7_evidence_required"]; u = dm["ub_depth_only_fills_on"]
body = f"""<h1>perp_perp_maker_taker_v1_1 — switch ladder</h1>
<p><span class=tag>EXPLORATORY</span> Two partial days (2022-01-05 and 2022-01-13), SOL perp, maker orders rest on either venue (about half on Bybit, half on Binance) and the taker hedge goes to the other one, latency preset singapore, Bybit perp mm fees, fault scenario clean, size 1, seed 1. A handful of fills decides each row: read the direction and the cause mix, not the dollar amounts.</p>
<p><span class=tag>queue parameters: SPOT prior, perp-unvalidated</span> Every queue, cancel and fill-evidence parameter in every row was calibrated on Bybit spot orders and has not been validated on the perp target (<code>queue_parameter_status</code> is carried in every result).</p>

<h2>1. Headline</h2>
<div class=note>On 2022-01-05, v1 reports <b>{b['fills']}</b> maker fills and <b>{b['cash']:+.2f}</b> USDT cash P&amp;L. Only <b>{e['fills']}</b> of them survive the evidence-required rule of step 7 (cash <b>{e['cash']:+.2f}</b>). The other {b['fills']-e['fills']} no longer fill under the stricter rule: {b['c_vanish']} of them were delta-vanish fills (a depth decrease or vanished level with no print), and the trade-caused count also falls ({b['c_trade']} to {e['c_trade']}) once depth decreases stop clearing queue ahead. On 2022-01-13 the same switch takes {s13['s0_v1_baseline']['fills']} fills to {s13['s7_evidence_required']['fills']} and cash {s13['s0_v1_baseline']['cash']:+.2f} to {s13['s7_evidence_required']['cash']:+.2f}. The sign of the P&amp;L flips on both days. Steps 2, 3, 4 and 6 move nothing material here; steps 5 and 7 do the work.</div>

<h2>2. Ladder, 2022-01-05 (cumulative, one switch per step)</h2>
<p>Each cell shows the value and, in brackets, the change from the previous row. Step 1 (manifest gate and truncation count) is not a row of its own: it changes no replay behaviour, only blocks a run on a truncated or unreconciled day and records the count, so by construction its row equals step 0 (no separate replay was run). Step 0 ran without the manifest; every later row runs with it (the cumulative step 1) and the written truncation reason. The truncation count is reported in every row.</p>
{table(ladder_rows)}
<p><small>Markouts pool all fills (maker legs and hedge legs on both venues, quantity-weighted, mid-based; n = {b['mkn']} at step 0 and {e['mkn']} at step 7 on 01-05). The tracker does not store a per-venue split, so a per-venue figure is not available from these runs. Maker fills by cause are the fill sources recorded in the maker observation log. “time net-unhedged” is the time the two venues’ positions did not net to zero. Breaker trips: none in any run.</small></p>

<h2>3. Ladder, 2022-01-13 (steps 0 and 7 only)</h2>
{table(d13_rows)}
<p>Steps 2–6 were not replayed on 01-13 (time cut). Whether a given step is responsible for the 01-13 change is therefore not separated; only the end-to-end v1 → v1.1 difference is measured.</p>

<h2>4. What each step did (2022-01-05)</h2>
<ul>
<li><b>Step 2, multiset dedupe:</b> no change to any reported figure on this day.</li>
<li><b>Step 3, ledger epoch:</b> {s05['s2_dedupe_multiset']['fills']} → {s05['s3_ledger_epoch']['fills']} fills (one delta-vanish fill gone); cash and equity unchanged to 4 dp; ambiguous-association flags {s05['s2_dedupe_multiset']['amb']} → {s05['s3_ledger_epoch']['amb']}.</li>
<li><b>Step 4, own-impact overlay:</b> no change on this day.</li>
<li><b>Step 5, no tape repair:</b> {s05['s4_own_overlay']['fills']} → {s05['s5_no_tape_repair']['fills']} fills, cash {s05['s4_own_overlay']['cash']:+.3f} → {s05['s5_no_tape_repair']['cash']:+.3f}; the cause mix shifts (delta-vanish {s05['s4_own_overlay']['c_vanish']} → {s05['s5_no_tape_repair']['c_vanish']}, trade {s05['s4_own_overlay']['c_trade']} → {s05['s5_no_tape_repair']['c_trade']}).</li>
<li><b>Step 6, fitted unknown queue:</b> no change, and no change was possible: no order in these runs was placed at a price outside the captured depth (0 orders with unknown queue; fit-missing fallbacks = {s05['s6_fitted_unknown_queue']['fitmiss']}). The fit exists and is flagged where it has no data (Binance 01-05 has 7 samples), but v1 never needed it on these two days.</li>
<li><b>Step 7, evidence-required fills:</b> {s05['s6_fitted_unknown_queue']['fills']} → {e['fills']} fills; the 21 delta-vanish fills are gone and trade-caused fills drop {s05['s6_fitted_unknown_queue']['c_trade']} → {e['c_trade']}; cash {s05['s6_fitted_unknown_queue']['cash']:+.3f} → {e['cash']:+.3f}. The 2 remaining non-cross fills are trade-print fills. The 3 opposite-cross fills remain (book-only fills, see §7).</li>
</ul>

<h2>5. Queue and cancel models on v1.1 (2022-01-05)</h2>
{plain_table(d_rows)}
<p><small>* cohort_uniform runs with <code>opposite_cross_fills</code> off, because the cohort queue refuses that switch (engine check). Its 3 book-only crosses therefore disappear and appear as 3 more trade fills, so the row is not a like-for-like comparison with the other three.</small></p>
<div class=note>behind_first, ahead_first and the calibrated proportional model give <b>identical</b> fills, trades and P&amp;L under v1.1: once a depth decrease alone cannot fill an order, the choice of how the queue ahead is depleted no longer decides any fill on this day (the 5 fills come from prints and book crosses). The model choice does change how many fills are suppressed for lack of evidence (suppressed quantity {sa['qv2']['maker_diagnostics']['evidence_suppressed_fill_qty']:.0f} for proportional against 106 for behind_first and 118 for ahead_first). Cohort uniform: 5 fills, all trade-caused, cash {dm['d_cohort_uniform']['cash']:+.3f}.</div>

<h2>6. Depth-only fills — upper-bound scenario, never the headline</h2>
{plain_table(ub_rows)}
<div class=note bad>This is v1.1 with <code>vanish_clears_queue</code> on and <code>fill_evidence = confirm_delta</code> — the step-6 configuration. It is identical to the step-6 row. It is shown as an <b>upper bound</b>: it adds {u['fills']-e['fills']} fills over v1.1, most of them orders for which the tape shows only a depth decrease. It must not be quoted as the strategy result.</div>

<h2>7. Checks and caveats</h2>
<ul>
<li><b>Config equality:</b> the settings of the step-7 replay (<code>ledger_epoch</code>, <code>unknown_queue_dist/floor/fallback</code>, <code>vanish_clears_queue</code>, <code>fill_evidence</code>, <code>trade_dedupe</code>, <code>own_impact</code>, <code>book_tape_repair</code>) equal the values in <code>profiles/perp_perp_maker_taker_v1_1.yaml</code>, key by key. The only keys that differ between the v1 and v1.1 yaml files are those switches plus the name, description and decision-status notes. The fit file and manifest path are supplied by the runner per day, not by the profile. v1’s yaml is untouched (sha256 2c70171b…cbdb4). No replay was run from the profile file itself.</li>
<li><b>Step 0 is a fresh live-v1 run</b> (<code>opposite_cross_fills</code> on). The earlier Phase 0 reference had it off, so the cause counts here do not match the audit’s 25/36 and 30/45.</li>
<li><b>Step 4</b> uses the overlay with <code>consume_liquidity</code> left on.</li>
<li><b>Step 7 keeps <code>opposite_cross_fills</code></b> (a fill when the market crosses the order’s price, from the book only, no print): it is reported as its own cause column. It is {e['c_cross']} of the {e['fills']} v1.1 fills on 01-05 and 9 of 20 on 01-13, so 45% of the v1.1 fills on 01-13 and 3 of 5 on 01-05 still rest on book evidence only. Turning it off is the next switch worth testing.</li>
<li><b>Data integrity:</b> one Binance trade tape is truncated on each day (~17 h of 24 h); the rows read before the cut are used, nothing after it exists, and the count is in every row. Both days are run under a written exploratory override.</li>
<li><b>Rebuild acceptance (touch price match, engine book):</b>
<table><thead><tr><th>venue / day</th><th>tape repair on</th><th>tape repair off (step 5)</th></tr></thead><tbody>
{"".join(f"<tr><td class=l>{v} {d}</td><td>{a:.2f}%</td><td>{o:.2f}%</td></tr>" for (v, d), (a, o) in acc.items())}</tbody></table>
Bybit 01-05 passes the 99% bar either way; Bybit 01-13 (98.73%) and both Binance days (91.52%, 88.30%) stay below it with repair off. Frame-atomic uncross was not started (decision F: only if step 5 leaves acceptance under 99% — it does, but it stays out of this task and needs your go-ahead).</li>
<li><b>Not run (time cut):</b> step 1 as a separate replay (equal to step 0 by construction); 01-13 steps 2–6; 01-13 queue-model and upper-bound runs; a replay from the v1.1 profile file; the sensitivity grid and the Coinbase lab (decision F).</li>
<li>Every P&amp;L in this report is <span class=tag>EXPLORATORY</span>.</li>
</ul>
"""
OUT.write_text(f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>v1.1 switch ladder</title><style>{css}</style></head><body>{body}</body></html>')
print("wrote", OUT, OUT.stat().st_size)
