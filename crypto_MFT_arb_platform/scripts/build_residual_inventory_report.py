import json
import subprocess
from pathlib import Path

PROJECT = Path("/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform")
EV = PROJECT / "reports/inventory_residual_evidence"
S = json.loads((EV / "evidence_summary.json").read_text())
OUT = PROJECT / "reports/residual_inventory_report.html"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def tbl(headers, rows, cls="", align=None):
    align = align or ["l"] + ["r"] * (len(headers) - 1)
    head = "".join(f'<th class="{a}">{h}</th>' for h, a in zip(headers, align))
    body = "".join("<tr>" + "".join(f'<td class="{a}">{c}</td>' for c, a in zip(r, align)) + "</tr>" for r in rows)
    return f'<table class="{cls}"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def pill(t, k="ok"):
    return f'<span class="pill {k}">{t}</span>'


def f(v, d=2):
    return f"{v:,.{d}f}"


tests = S["tests"]
paper = S["paper_residual"]
rr = S["replay_residual"]["rows"]
inv = S["replay_inventory"]["rows"]
syn = S["synthetic_drain"]
acc = S["acceptance"]
commits = S["code"]["commits"]

WHAT = {
    "test_balanced_package_reserves_its_full_leg_quantity":
        "A flat BUY 0.01 / SELL 0.01 package reserves 0.01 (not 0). With a 0.015 legging limit the 2nd package is refused; after releasing the 1st it is admitted.",
    "test_carried_residual_counts_against_legging_exposure":
        "Carried residual −0.006 + new 0.01 > 0.015 limit → refused with 'max legging exposure exceeded'.",
    "test_new_risk_is_blocked_while_carried_residual_is_at_the_limit":
        "Carried residual 0.002 = max_residual_base 0.002 → all new risk refused ('carried residual at limit').",
    "test_a_residual_that_offsets_the_carried_one_is_accepted":
        "Carried +0.01, cap 0.015: a further +0.01 is NOT within bound; a −0.01 that offsets it IS.",
    "test_residuals_within_bound_one_at_a_time_cannot_pile_up":
        "Cap 0.02, Bybit leg always rejected: plan 1 closes holding 0.01; plan 2 would make 0.02 → it unwinds (UNWIND child) and closes flat. Carried and total residual stay 0.01.",
    "test_hedged_plan_pnl_is_unchanged":
        "Fully hedged plan: residual 0, mark value 0, net_pnl = gross − fees (no change).",
    "test_open_residual_is_marked_not_booked_as_cash":
        "Orphan buy 0.01 @ 60,000 (cash −600): residual marked at mid 60,030 → mark value 600.30, |net_pnl| < 1 instead of −600.",
    "test_buy_without_quote_on_the_buy_venue_is_rejected":
        "BINANCE holds 500 USDT; buying 0.01 × 60,000 = 600 → 'insufficient quote inventory: BINANCE'.",
    "test_margin_enabled_sell_venue_needs_no_base":
        "BYBIT margin-enabled with 0 BTC: sell admitted under margin checks, no base reserved.",
    "test_reserved_base_is_held_until_release":
        "BYBIT holds 0.015: 1st sell reserves 0.01, 2nd refused; release frees it; snapshot shows reserved 0.01 / available 0.005.",
    "test_sell_without_base_on_the_sell_venue_is_rejected":
        "BYBIT holds 0 BTC → selling 0.01 refused ('insufficient base inventory: BYBIT').",
    "test_unconstrained_model_keeps_the_legacy_behaviour":
        "No inventory policy: no base reserved; label UNCONSTRAINED_SPOT_SHORTING_ALLOWED.",
    "test_fills_move_inventory_and_one_way_trading_runs_out_of_base":
        "Through the OMS + ledger: after one BUY BINANCE / SELL BYBIT package, BYBIT 0.01→0, BINANCE 0→0.01; the same direction is refused (no fills); the reverse trade is admitted and restores balances.",
    "test_one_directional_arb_drains_a_venue_shared_with_mean_reversion":
        "paper_watch config (pre-funded 0.05) on 6,000 synthetic events: base never negative, BINANCE+BYBIT = 0.10 BTC conserved, every refusal is 'insufficient base inventory: BYBIT'.",
    "test_missing_section_is_unconstrained":
        "Config without risk.inventory → UNCONSTRAINED.",
    "test_pre_funded_config_is_parsed_and_labelled":
        "pre_funded with 0.05 BTC/venue parsed; quote defaults to initial_capital_by_venue (10,000); label PRE_FUNDED_INVENTORY_NO_REBALANCING.",
    "test_pre_funded_needs_a_base_balance_for_every_venue":
        "pre_funded missing a venue's base balance → config error.",
}

test_rows = [[c["file"].replace(".py", ""), c["class"], f"<code>{c['test']}</code>", WHAT.get(c["test"], ""),
              pill(c["status"], "ok" if c["status"] == "PASSED" else "bad"), f"{c['seconds']:.3f}"] for c in tests["cases"]]

before = {r["cycle"]: r for r in rr if r["run"] == "before"}
after = {r["cycle"]: r for r in rr if r["run"] == "after"}
replay_rows = []
for k in sorted(before):
    b, a = before[k], after[k]
    replay_rows.append([k, f"{b['paper_end_residual_base']:+.4f}", f"{b['end_residual_base']:+.4f}", f"<b>{a['end_residual_base']:+.4f}</b>",
                        f(b["net_pnl"]), f(b["equity_pnl"]), f"<b>{f(a['net_pnl'])}</b>", f(a["equity_pnl"]),
                        f"{b['unwind_fills']} → <b>{a['unwind_fills']}</b>",
                        f"{b['residual_within_bound']} → {a['residual_within_bound']}",
                        f"{b['opportunities']} → {a['opportunities']}", a["kill_switch"]])

inv_rows = [[r["cycle"], r["initial_base_per_venue"], f"{r['opportunities_unconstrained']} → {r['opportunities_pre_funded']}",
             r["risk_refusals"], f(r["equity_pnl_unconstrained"]), f(r["equity_pnl_pre_funded"]),
             r["end_base_binance"], r["end_base_bybit"]] for r in inv]

worst = sorted(paper["rows"], key=lambda r: -abs(r["overstatement"]))[:10]
paper_rows = [[r["cycle"][6:], f(r["headline_net_pnl"]), f(r["equity_pnl"]), f(r["overstatement"]), f"{r['end_residual_base']:+.4f}"] for r in worst]
all_paper_rows = [[r["cycle"][6:], f(r["headline_net_pnl"]), f(r["equity_pnl"]), f"{r['end_residual_base']:+.4f}"] for r in paper["rows"]]

pf, un = syn["pre_funded_0.05"], syn["unconstrained"]
commit_rows = [[f"<code>{c['sha']}</code>", c["subject"], "<pre class='stat'>" + c["stat"] + "</pre>"] for c in commits]
files_rows = [[f"<code>{k}</code>", f"{v['bytes']:,}", f"<code>{v['sha256'][:16]}…</code>"] for k, v in S["files"].items()]

html = f"""<!doctype html><html><head><meta charset="utf-8"><title>Residual Exposure &amp; Spot Inventory – Report</title>
<style>
@page {{ size: A4; margin: 15mm 13mm; }}
body {{ font-family: -apple-system, "Helvetica Neue", Arial, sans-serif; color:#111827; font-size:9.8pt; line-height:1.45; margin:0; background:#fff; }}
h1 {{ font-size:22pt; margin:0 0 4px; }} h2 {{ font-size:14pt; margin:20px 0 8px; padding-bottom:4px; border-bottom:2px solid #1d4ed8; }}
h3 {{ font-size:11pt; margin:13px 0 6px; }} p {{ margin:5px 0; }} .sub {{ color:#4b5563; }}
.cover {{ border-left:5px solid #1d4ed8; padding:6px 0 6px 14px; margin-bottom:12px; }}
.kpis {{ display:grid; grid-template-columns:repeat(4,1fr); gap:8px; margin:10px 0; }}
.kpi {{ border:1px solid #e5e7eb; border-radius:6px; padding:8px 10px; background:#f3f4f6; }} .kpi b {{ display:block; font-size:14pt; }} .kpi span {{ color:#4b5563; font-size:8.4pt; }}
table {{ width:100%; border-collapse:collapse; margin:6px 0 10px; font-size:8.6pt; }} tr {{ page-break-inside:avoid; }}
th {{ background:#1f2937; color:#fff; padding:4px 5px; text-align:left; font-weight:600; }} td {{ padding:3px 5px; border-bottom:1px solid #e5e7eb; vertical-align:top; }}
tbody tr:nth-child(even) td {{ background:#fafafa; }} .small {{ font-size:7.6pt; }} .r {{ text-align:right; }} .l {{ text-align:left; }} .c {{ text-align:center; }}
code {{ font-family:Menlo,monospace; font-size:7.9pt; background:#f3f4f6; padding:0 3px; border-radius:3px; }}
pre {{ font-family:Menlo,monospace; font-size:7.8pt; background:#f3f4f6; border:1px solid #e5e7eb; border-radius:5px; padding:7px 9px; white-space:pre-wrap; margin:4px 0; }}
pre.stat {{ border:none; padding:0; background:none; font-size:7pt; }}
.pill {{ display:inline-block; padding:0 6px; border-radius:9px; font-size:7.6pt; font-weight:600; }} .ok {{ background:#dcfce7; color:#166534; }} .bad {{ background:#fee2e2; color:#991b1b; }} .warn {{ background:#fef3c7; color:#92400e; }}
.callout {{ border:1px solid #bfdbfe; background:#eff6ff; border-radius:6px; padding:7px 11px; margin:7px 0; }} .callout.warn {{ border-color:#fde68a; background:#fffbeb; }}
.pb {{ page-break-before:always; }} ul {{ margin:4px 0 6px 18px; padding:0; }} li {{ margin:2px 0; }}
</style></head><body>
<div class="cover"><h1>Residual Exposure &amp; Spot Inventory</h1>
<div class="sub">Change report for <code>tests/test_residual_exposure.py</code> and <code>tests/test_spot_inventory.py</code> · crypto_MFT_arb_platform · 12 Oct 2026</div>
<div class="sub">GitHub: vaibhaw1994kumar/CryptoMFT · commits <code>40b52d4</code>, <code>fa70b81</code>, <code>8d66ce7</code> · evidence bundle <code>reports/inventory_residual_evidence.zip</code></div></div>

<div class="kpis">
<div class="kpi"><b>{tests['passed']} / {tests['item_tests']}</b><span>new tests passing (residual 7 + inventory 10)</span></div>
<div class="kpi"><b>250 / 250</b><span>crypto_stat_arb suite; offline acceptance gate PASSED</span></div>
<div class="kpi"><b>≤ 0.001 BTC</b><span>end residual on replayed cycles (was up to 0.007 vs 0.002 cap)</span></div>
<div class="kpi"><b>±0.2 USDT</b><span>plan net_pnl vs equity PnL (was up to 551 USDT apart)</span></div>
</div>

<h2>1. Summary</h2>
<p>Two capital/risk-layer gaps were fixed. <b>(1) Residual exposure:</b> a balanced two-leg package reserved zero residual capacity, the residual bound was checked per plan only, and a plan's PnL booked an unhedged leg's cash as profit — so recorded paper cycles ended holding up to 0.006–0.007 BTC naked against a 0.002 limit, and the headline paper PnL across 44 cycles read <b>+{f(paper['sum_headline_net_pnl'])} USDT</b> while the marked equity change was <b>{f(paper['sum_equity_pnl'])} USDT</b>. <b>(2) Spot inventory:</b> a spot sell was admitted against the position limit only, so a venue holding no BTC could sell (on the synthetic stream BYBIT sold <b>0.080 BTC it never held</b>). Both are fixed, tested and enabled in the platform configs.</p>

<h2>2. Problems found (code before the change)</h2>
{tbl(["#", "Where", "Behaviour", "Consequence"], [
 ["1", "<code>risk.py</code> reserve(): <code>planned_residual = abs(sum(positions))</code>", "BUY +q / SELL −q ⇒ 0 reserved", "Any number of packages could leg out simultaneously"],
 ["2", "<code>oms.py</code> <code>_residual_within_bound</code>", "<code>abs(plan.residual) &lt; cap</code> per plan", "Residuals of 0.001 each 'within bound' piled up (0.003–0.007 open)"],
 ["3", "<code>oms.py</code> <code>residual_risk_snapshot</code>", "<code>open_residual_base</code> counted only non-CLOSED plans", "Residual left by closed plans invisible to monitoring (no CRITICAL alert)"],
 ["4", "<code>oms.py</code> <code>_recompute_plan_economics</code>", "<code>net_pnl = Σ sell cash − Σ buy cash − fees</code>", "Orphan leg booked as ±78 USDT per 0.001 BTC"],
 ["5", "<code>risk.py</code> reserve(): <code>cash = {{buy_venue: notional}}</code>", "Sell leg checked only vs <code>max_abs_position_base</code>", "Venue with 0 BTC could sell (implicit short)"],
 ["6", "<code>risk.py</code> <code>AccountRiskSnapshot</code>", "Static <code>available_cash</code> (usually ∞); <code>update_account_snapshot</code> never called", "Risk never saw balances move with fills"],
], align=["c","l","l","l"])}

<h2>3. Changes</h2>
<h3>3.1 Residual exposure (commit 40b52d4)</h3>
<pre>Admission (risk.py):
  planned_residual = max(|leg quantity|)                         # worst case, not the flat net
  refuse if |carried| ≥ max_residual_base                          → "carried residual at limit"
  refuse if reserved_residual + planned_residual + |carried|
            &gt; max_legging_exposure_base (default max_position_base) → "max legging exposure exceeded"

Residual bound (oms.py):
  carried = Σ residual_base of CLOSED plans                        # derived from plans ⇒ restart-safe
  within  = residual == 0
            or ( |residual| &lt; cap and ( |carried + residual| &lt; cap or |carried + residual| ≤ |carried| ) )
  not within ⇒ HEDGE_UNWIND → unwind order, instead of closing with a naked leg

Marked PnL (oms.py, models.py):
  residual_mark_price = mean mid of the instrument's valid books (fallback: signal prices)
  net_pnl = gross_pnl + residual_base × residual_mark_price − fees − unwind_cost

Visibility: snapshot carried_residual_base, total_residual_base; monitoring CRITICAL on total_residual_base;
            analyze-session WARN "no_carried_residual"; config risk.max_legging_exposure_base.</pre>
<h3>3.2 Spot inventory (commits fa70b81, 8d66ce7)</h3>
<pre>risk.inventory.model: unconstrained (legacy default)  |  pre_funded
held_base(v)  = initial_base_by_venue[v]  + ledger.positions[(v, instrument)]
held_quote(v) = initial_quote_by_venue[v] + ledger.cash_deltas[v]       # quote default: initial_capital_by_venue
pre_funded, non-margin venues:
  BUY leg : reserved_quote(buy_venue) + buy_notional ≤ held_quote(buy_venue)  else "insufficient quote inventory: V"
  SELL leg: reserved_base(sell_venue) + quantity     ≤ held_base(sell_venue)  else "insufficient base inventory: V"
margin-enabled venue: existing margin checks, no base reserved
Reservation.base_by_venue released with the package; summaries report inventory model, label and balances.
Configs research.json / paper_watch.json: pre_funded, 0.05 BTC per venue + 10,000 USDT.</pre>
{tbl(["Commit", "Subject", "Files"], commit_rows, "small", ["l","l","l"])}

<h2 class="pb">4. Test results</h2>
<p>Fresh run: <b>{tests['passed']}/{tests['item_tests']} passed</b> (exit code {tests['exit_code']}); full suite: <b>{tests['full_suite']}</b>.</p>
{tbl(["File", "Class", "Test", "What it proves", "Result", "s"], test_rows, "small", ["l","l","l","l","c","r"])}

<h2 class="pb">5. Data and results</h2>
<h3>5.1 Recorded live paper cycles (evidence of the problem)</h3>
<p>{paper['cycles']} cycles: headline <code>net_pnl</code> sum <b>{f(paper['sum_headline_net_pnl'])}</b> USDT vs marked equity PnL <b>{f(paper['sum_equity_pnl'])}</b> USDT. {paper['cycles_with_end_residual']} cycles ended holding a residual; the largest was {paper['max_abs_end_residual']:.3f} BTC (cap 0.002). Ten largest overstatements:</p>
{tbl(["Cycle", "Headline net_pnl", "Equity PnL", "Overstatement", "End residual BTC"], paper_rows)}

<h3>5.2 Replay of cycles 0006–0010: before vs after the residual fix</h3>
{tbl(["Cycle", "Paper end res.", "Before end res.", "After end res.", "net_pnl before", "Equity before", "net_pnl after", "Equity after", "Unwind fills", "Within-bound closes", "Opps", "Kill switch"], replay_rows, "small")}
<div class="callout">After: end residual ≤ 0.001 BTC on every cycle (cap 0.002); 7–10 residual unwinds per cycle where there were none; <code>net_pnl</code> within 0.2 USDT of equity PnL; kill switch INACTIVE, no open breaches, no open plans. The unwinds cost 0.3–0.8 USDT per cycle (spread paid to close positions that used to stay open).</div>

<h3>5.3 Replay of cycles 0006–0010 with pre-funded inventory</h3>
{tbl(["Cycle", "BTC/venue", "Opps unconstrained → pre-funded", "Refused", "Equity unconstrained", "Equity pre-funded", "End BTC BINANCE", "End BTC BYBIT"], inv_rows, "small")}
<div class="callout">No package was refused at 0.05 or 0.01 BTC per venue; results are identical to unconstrained. The 0.001 BTC basis mean-reversion trades alternate direction, so base drifts ≤ 0.007 BTC per 30-minute cycle (closest: cycle 0010 BYBIT 0.003 at the 0.01 level).</div>

<h3>5.4 Synthetic one-directional stream (6,000 events, paper_watch strategies)</h3>
{tbl(["Inventory", "Plans (arb / MR)", "Refusals", "End BTC BINANCE", "End BTC BYBIT", "Ledger position BYBIT"], [
 ["pre-funded 0.05", f"{pf['plans_by_strategy'].get('cross_venue_btc_v1')} / {pf['plans_by_strategy'].get('basis_mean_reversion_v1')}", ", ".join(f"{k}: {v}" for k,v in pf['refusals'].items()), pf['inventory']['venues']['BINANCE']['held_base'], pf['inventory']['venues']['BYBIT']['held_base'], pf['ledger_positions'].get('BYBIT:BTC-USDT-SPOT')],
 ["unconstrained", f"{un['plans_by_strategy'].get('cross_venue_btc_v1')} / {un['plans_by_strategy'].get('basis_mean_reversion_v1')}", "none", "–", "–", f"<b>{un['ledger_positions'].get('BYBIT:BTC-USDT-SPOT')}</b> (sold without holding)"],
], "small", ["l","l","l","r","r","l"])}
<div class="callout warn">The synthetic arb is persistently BUY BINANCE / SELL BYBIT. Pre-funded: BYBIT's 0.05 BTC is drained after five 0.010 packages and every later package needing to sell there is refused — including the mean-reversion strategy's exits, which share the account. Unconstrained: BYBIT sells 0.080 BTC it never held.</div>

<h3>5.5 Acceptance gate</h3>
{tbl(["Item", "Value"], [
 ["Run", f"<code>{acc['run']}</code>"], ["Status", pill(acc['summary'].get('status'))],
 ["Tests", acc['summary'].get('tests')], ["Benchmark decision", acc['summary'].get('benchmark_decision')],
 ["Paper release id", f"<code>{acc['summary'].get('paper_release_id')}</code> (changed from bfbcab66…, expected after the config change)"],
] + [[f"<code>{k}</code>", f"{v['label']} · BTC {v['held_base']}"] for k, v in acc['labels'].items()], align=["l","l"])}

<h2>6. Known gaps</h2>
<ul>
<li>Quote reservation excludes fees; unwind (risk-reducing) orders are not blocked by inventory.</li>
<li>A package's reservation is held until the plan closes, so between fill and close balances are counted twice (conservative).</li>
<li>No inter-venue transfer/rebalancing model (results labelled <code>PRE_FUNDED_INVENTORY_NO_REBALANCING</code>); long one-directional runs exhaust a venue.</li>
<li>Reservations are released at plan close, not reduced leg by leg as fills arrive.</li>
<li>Two multi-strategy plumbing tests in <code>test_watch_pipeline.py</code> pin unconstrained inventory because the synthetic stream drains BYBIT.</li>
</ul>

<h2 class="pb">Appendix A – all 44 recorded paper cycles</h2>
{tbl(["Cycle", "Headline net_pnl", "Equity PnL", "End residual BTC"], all_paper_rows, "small")}
<h2>Appendix B – evidence bundle contents</h2>
{tbl(["File", "Bytes", "SHA-256"], files_rows, "small", ["l","r","l"])}
<pre>Regenerate: python3.12 scripts/collect_inventory_residual_evidence.py --replay-dir &lt;scratch&gt;/div --staging-repo &lt;clone&gt;</pre>
</body></html>"""
OUT.write_text(html, encoding="utf-8")
pdf = OUT.with_suffix(".pdf")
subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-pdf-header-footer", f"--print-to-pdf={pdf}", OUT.as_uri()],
               capture_output=True, timeout=120)
print(pdf, pdf.stat().st_size)
