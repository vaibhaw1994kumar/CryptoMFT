"""Builds /Users/vaibhav/Documents/Newproject/queue_model_v2_report.html from the artifacts under out/queue_v2 (nothing typed in by hand except the prose).
Every P&L is EXPLORATORY. usage: python3.12 build_report.py"""
import glob, html, json, math, os, sys
from pathlib import Path
sys.path.insert(0, "/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform/src")
X = Path("/Users/vaibhav/Documents/Newproject/out/queue_v2")
E = Path("/Users/vaibhav/Documents/Newproject/out/exec_layer")
OUT = Path("/Users/vaibhav/Documents/Newproject/queue_model_v2_report.html")
esc = html.escape

def jl(p, default=None):
    try: return json.loads(Path(p).read_text())
    except Exception: return default

def fmt(x, d=3):
    if x is None: return "–"
    if isinstance(x, float):
        if math.isnan(x): return "–"
        return f"{x:.{d}f}"
    return esc(str(x))

def pct(x, d=1): return "–" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{100 * x:.{d}f}%"

def ci(t, d=1): return "–" if not t else f"{pct(t[0], d)} [{pct(t[1], d)}, {pct(t[2], d)}]"

def table(head, rows, cls=""):
    h = "".join(f"<th>{esc(str(c))}</th>" for c in head)
    b = "".join("<tr>" + "".join(f"<td>{c if str(c).startswith('<') else esc(str(c))}</td>" for c in r) + "</tr>" for r in rows)
    return f'<table class="{cls}"><thead><tr>{h}</tr></thead><tbody>{b}</tbody></table>'

def sec(title, body, id_=""): return f'<section id="{id_}"><h2>{esc(title)}</h2>{body}</section>'

def p(t): return f"<p>{t}</p>"

parts = []

# ── header ───────────────────────────────────────────────────────────────────
parts.append(f'''<header><h1>Queue model v2 — a calibrated L2 queue model</h1>
<p class="sub">Generated {esc(__import__("time").strftime("%Y-%m-%d %H:%M"))}. Scope: maker fill simulation (the PASSIVE tactic's fill model). Binance and Bybit publish no L3, so everything here is a <b>calibrated L2 queue model</b> with explicit assumptions —
never an “L3 reconstruction”; cohort labels are model labels, not recovered orders. Every P&amp;L number is <b>EXPLORATORY</b> (two days, clean preset, size 1).</p></header>''')

# ── gate ─────────────────────────────────────────────────────────────────────
g = jl(E / "gate_p0i_result.json", {})
m = jl(E / "mutation_evidence_p0i.json", {})
rows = [[k, "identical" if v.get("identical_to_base_files") else "DIFFERS", len(v.get("summary_diffs_vs_base_ex_preflight", [])), v.get("reconciliation_ok")] for k, v in g.items() if k != "GATE0_PASS"]
parts.append(sec("0. Precondition: Phase 0 gate (quoted from the evidence files)", table(["replay", "trade log / fills / bucket stats / model settings vs pre-refactor", "summary diffs (ex preflight)", "reconciliation ok"], rows)
    + p(f"GATE0_PASS = <b>{g.get('GATE0_PASS')}</b>. Mutations caught: <b>{sum(1 for k, v in m.items() if v.get('caught'))} of {sum(1 for k in m if not k.startswith('_'))}</b>; control: {esc(str(m.get('_control_unmutated', {}).get('summary')))}. Full suite 1608 passed, 1 skipped, 7 xfailed (2026-11-03 evening).")
    + p("The Pairing / Venue Selector / Risk Gate brief has not been supplied; per the user, Phase 0 was treated as the gate for this brief."), "gate"))

# ── step 0 ───────────────────────────────────────────────────────────────────
parts.append(sec("1. Step 0 — module map", p("Full map with file/function references: <code>out/queue_v2/step0_module_map.md</code>.")
    + table(["guide module", "existing code (extended, not rewritten)", "main gap closed here"], [
        ["ProviderAdapter", "event_loader.py, quality_preflight.py", "manifest + truncation gate (step 1)"],
        ["BookRebuilder", "order_book.py", "exact fixed-point rebuild + delta-only acceptance; tape-repair switch; capture event order; shadow overlay (step 2)"],
        ["ObservationClock", "cross_venue_feed, timing, truth books, exchange-time eligibility", "pinned by fixtures (step 3)"],
        ["DepletionLedger", "fill_engine trade/depth netting credits", "epoch expiry + ambiguity flag behind ledger_epoch; 4 fixtures (step 4)"],
        ["QueueModel", "MakerConfig/MakerExecutor (all_ahead, proportional, power)", "behind_first, ahead_first, min_baseline name, cohort queue, fitted unknown queue (steps 5–7)"],
        ["ExecutionAdapter", "place/cancel/amend, PostOnly, lifecycle", "provenance record (step 9); invariants (step 10)"],
        ["CalibrationReport", "fill_label, fe_* study pipeline", "step 11 harness on the real spot orders"]]), "map"))

# ── manifest + acceptance ───────────────────────────────────────────────────
man = jl(X / "manifest.json", {})
cnt = {}
for k, v in (man.get("days") or {}).items():
    cnt[(k.split("/")[0], v["status"])] = cnt.get((k.split("/")[0], v["status"]), 0) + 1
body = p(f"{man.get('n_files')} files, {len(man.get('days', {}))} venue-days (<code>out/queue_v2/manifest.json</code>; provider, exchange id and product are <i>declared</i>, not derived). A truncated day blocks a run unless <code>allow_rejected_data</code> carries a written reason, which is then recorded in the result with the manifest hash.")
body += table(["venue dir", "status", "venue-days"], [[a, b, n] for (a, b), n in sorted(cnt.items())])
acc = []
for f in sorted(glob.glob(str(X / "rebuild_acceptance" / "*.json"))):
    d = jl(f)
    for name, v in d["variants"].items():
        acc.append([f"{d['venue']} {d['day']}", d["event_order"], name, v["snapshots"], f"{v['touch_price_match_pct']:.2f}%", f"{v['touch_size_match_pct']:.2f}%", f"{v['top10_level_match_pct']:.2f}%",
                    "FLAG <99%" if (name == "engine_tape_repair_on" and d.get("flag_below_99")) else ""])
body += "<h3>Rebuild acceptance (delta-only between consecutive 1 s snapshots, compared with every reference snapshot)</h3>"
body += table(["venue-day", "event order", "variant", "snapshots", "touch price", "touch price+size", "top-10 levels", "flag"], acc)
body += p("Reading: the touch <i>price</i> matches 99.3 % / 98.7 % (Bybit 01-05 / 01-13) and 91.5 % / 88.3 % (Binance); size agreement is lower because the snapshot is a top-13/19 sample taken between messages. Tape repair on versus off moves the touch size match by about −1.4 pp (Binance 01-05) and +0.4 pp (Bybit); the capture event order makes almost no difference on these days. The Oct-2024 Binance snapshots are not a usable reference (touch price match 2 % on 2024-10-05: flat 50.0 sizes on a 0.01 grid against 0.001-tick deltas).")
parts.append(sec("2. Steps 1–2 — manifest, exact rebuild, acceptance", body, "manifest"))

# ── junit evidence ──────────────────────────────────────────────────────────
junit = jl(X / "junit_summary.json", {})
def jtable(prefix):
    rows = [[t["name"], t["outcome"]] for t in junit.get("tests", []) if t["file"].startswith(prefix)]
    return table(["test", "outcome"], rows) if rows else p("(run build_junit.py)")
parts.append(sec("3. Step 3 — one trade-confirmed hypothetical order (fixed latency, fixtures)", jtable("test_queue_v2_hypothetical_order"), "step3"))
parts.append(sec("4. Step 4 — depletion ledger fixtures (both arrival orders) and the double-depletion case", p("Result of the 50 ms association against the fixtures: every book-first / trade-first / credit-once / window fixture passes on today's mechanism. The one failing fixture was <i>credit survives a snapshot</i> (legacy keeps it); fixed behind <code>ledger_epoch</code>. OrderBook tape repair reproduces the double depletion when the depth decrease comes first.") + jtable("test_queue_v2_ledger"), "step4"))

# worked example numbers recomputed here
try:
    from new_backtest.queue_v2.cancel_models import cancel_ahead
    from new_backtest.queue_v2.cohort import LevelQueue
    rows = []
    for name in ("behind_first", "proportional", "ahead_first"):
        C_ahead = cancel_ahead(name, 4.0, 10.0, 5.0)
        ahead = 10.0 - C_ahead
        fill = min(2.0, max(0.0, 8.0 - ahead))
        rows.append([name, f"{C_ahead:.4f}", f"{ahead:.4f}", f"{fill:.4f}"])
    wex = table(["model", "C_ahead", "ahead after the cancel", "own fill after an 8-unit print"], rows)
except Exception as ex:
    wex = p(f"(could not compute: {esc(str(ex))})")
parts.append(sec("5. Step 5 — cancellation models by their arithmetic", p("C is the NET residual cancellation (displayed decrease less the part prints explained), A external ahead, B external behind; feasibility max(0, C−B) ≤ C_ahead ≤ min(C, A); zero totals never divide. "
    "behind_first = min(A, max(0, C−B)); ahead_first = min(C, A); proportional = C·A/(A+B); power(n) = C·Aⁿ/(Aⁿ+Bⁿ); min_baseline = today's all_ahead (old name kept as a deprecated alias).") + "<h3>The guide's worked example (ahead 10, behind 5, own 2, residual cancel 4, then an 8-unit print)</h3>" + wex + jtable("test_queue_v2_cancel_models"), "step5"))
parts.append(sec("6. Step 6 — cohort queue", p("External size at our price is kept as cohorts by arrival; the displayed size when we joined is one cohort of <b>unknown age (assumed 30 s — an assumption, not a measurement)</b>; later additions join behind us. Residual cancellations are removed by an age hypothesis (youngest_first, oldest_first, uniform, hazard(θ) with weight size·(1+age)^−θ). Several own orders at one price share one traversal and one print budget; a snapshot re-bases the external size and never cancels an own order; projections are logged.")
    + jtable("test_queue_v2_cohort"), "step6"))

# ── unknown queue ───────────────────────────────────────────────────────────
uq = ""
for d in ("2022-01-05", "2022-01-13"):
    f = jl(X / f"unknown_queue_fit_for_{d}.json")
    if not f: continue
    for v, fit in f["venues"].items():
        for b in fit["buckets"]:
            uq += f"<tr><td>{d}</td><td>{esc(v)}</td><td>{b['d_lo']}–{b['d_hi'] if b['d_hi'] < 1e5 else '∞'}</td><td>{b['n']}</td><td>{fmt(b['median'], 1)}</td><td>{fmt(b['sigma'], 2)}</td></tr>"
uqt = f'<table><thead><tr><th>test day (train strictly before)</th><th>venue</th><th>ticks from touch</th><th>n levels</th><th>median size</th><th>log-σ</th></tr></thead><tbody>{uq}</tbody></table>'
parts.append(sec("7. Step 7 — unknown queue", p("An absent level inside the captured depth or the spread is empty (queue 0). Outside the captured depth the queue is unknown: the fixed 25-unit floor is replaced (opt-in) by a lognormal fitted on in-depth level sizes at comparable distance from the touch, training days only, one seeded draw per order, every such fill flagged <code>queue_unknown</code>. No Binance snapshot rows exist for the only day before 2022-01-05 (2022-01-04 is empty), so Binance falls back to the floor for that test day and says so.") + uqt + jtable("test_queue_v2_invariants::IndependentCases"), "step7"))

parts_after = []
OUT_PARTS = parts

def finish(extra):
    css = """body{font:14px/1.45 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:1180px;margin:24px auto;padding:0 16px;color:#1b1f24;background:#fff}
h1{font-size:24px}h2{font-size:18px;margin-top:34px;border-bottom:1px solid #d0d7de;padding-bottom:4px}h3{font-size:15px;margin-top:20px}
table{border-collapse:collapse;margin:8px 0 14px;font-size:12.5px}th,td{border:1px solid #d0d7de;padding:3px 8px;text-align:left;vertical-align:top}th{background:#f6f8fa}
code{background:#f6f8fa;padding:1px 4px;border-radius:3px}.sub{color:#57606a}.warn{background:#fff8c5;border:1px solid #d4a72c;padding:8px 12px;border-radius:6px}
@media(prefers-color-scheme:dark){body{background:#0d1117;color:#e6edf3}th{background:#161b22}th,td{border-color:#30363d}code{background:#161b22}.warn{background:#2d2a14;border-color:#9e6a03}h2{border-color:#30363d}.sub{color:#8b949e}}"""
    doc = f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Queue model v2 report</title><style>{css}</style></head><body>' + "".join(OUT_PARTS) + "".join(extra) + "</body></html>"
    OUT.write_text(doc)
    print("written", OUT, len(doc) // 1024, "KB")

if __name__ == "__main__":
    ex = []
    exec(open(X / "build_report_part2.py").read()) if (X / "build_report_part2.py").exists() else None
    finish(ex)
