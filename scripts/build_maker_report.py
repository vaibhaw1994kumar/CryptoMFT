"""Build the execution-realism improvements report (HTML -> PDF via headless Chrome)."""
import json
import subprocess
from html import escape
from pathlib import Path

PROJECT = Path("/Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform")
SWEEP = PROJECT / "runs/maker_sensitivity/20261011T172155Z/summary.json"
OUT_DIR = PROJECT / "reports"
OUT_DIR.mkdir(exist_ok=True)
HTML = OUT_DIR / "maker_execution_realism_report.html"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

sweep = json.loads(SWEEP.read_text())
table = sweep["table"]
EVIDENCE = sorted((PROJECT / "reports/evidence").glob("2*T*Z"))[-1]
evidence = json.loads((EVIDENCE / "evidence_summary.json").read_text())
checks = evidence["checks"]
integrity = checks["0_data_integrity"]["result"]
audit = checks["6_session_audit"]["result"]
parity = checks["1_engine_parity"]["result"]


def lag_row(venue, lag, frac=1.0):
    return next(r for r in table if r["venue"] == venue and r["feed_lag_ms"] == lag and r["queue_position_frac"] == frac)


def pct(new, old):
    return (new / old - 1) * 100


D = {}
for venue in ("BINANCE", "BYBIT"):
    base = lag_row(venue, 0)
    for lag in (35, 75):
        row = lag_row(venue, lag)
        D[(venue, lag)] = {
            "qty": pct(row["fill_qty"], base["fill_qty"]),
            "rate": pct(row["fill_rate"], base["fill_rate"]),
            "mk": row["markout_1s_bps"] - base["markout_1s_bps"],
        }
table = sweep["table"]
meta = sweep["meta"]


def tbl(headers, rows, cls="", align=None):
    align = align or ["l"] + ["r"] * (len(headers) - 1)
    head = "".join(f'<th class="{a}">{h}</th>' for h, a in zip(headers, align))
    body = "".join(
        "<tr>" + "".join(f'<td class="{a}">{c}</td>' for c, a in zip(row, align)) + "</tr>"
        for row in rows
    )
    return f'<table class="{cls}"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def pill(text, kind):
    return f'<span class="pill {kind}">{text}</span>'


def f(v, d=2):
    return "–" if v is None else f"{v:,.{d}f}"


# --- charts ------------------------------------------------------------------
SERIES = [
    ("BINANCE", 1.0, "#2563eb", ""), ("BINANCE", 0.7, "#2563eb", "5 4"),
    ("BYBIT", 1.0, "#ea580c", ""), ("BYBIT", 0.7, "#ea580c", "5 4"),
]
LAGS = meta["lags_ms"]


def line_chart(metric, title, ylabel, transform=lambda v: v, y_fmt="{:.0f}"):
    W, H, L, R, T, B = 640, 280, 56, 150, 30, 42
    pts = {}
    for venue, frac, *_ in SERIES:
        rows = sorted((r for r in table if r["venue"] == venue and r["queue_position_frac"] == frac),
                      key=lambda r: r["feed_lag_ms"])
        pts[(venue, frac)] = [(r["feed_lag_ms"], transform(r[metric])) for r in rows]
    ys = [y for series in pts.values() for _, y in series]
    lo, hi = min(ys), max(ys)
    pad = (hi - lo) * 0.12 or 1
    lo, hi = lo - pad, hi + pad
    sx = lambda x: L + (x - min(LAGS)) / (max(LAGS) - min(LAGS)) * (W - L - R)
    sy = lambda y: T + (hi - y) / (hi - lo) * (H - T - B)
    out = [f'<svg viewBox="0 0 {W} {H}" class="chart" xmlns="http://www.w3.org/2000/svg">',
           f'<text x="{L}" y="18" class="ct">{title}</text>']
    for i in range(5):
        y = lo + (hi - lo) * i / 4
        out.append(f'<line x1="{L}" x2="{W-R}" y1="{sy(y):.1f}" y2="{sy(y):.1f}" class="grid"/>'
                   f'<text x="{L-8}" y="{sy(y)+4:.1f}" class="tick" text-anchor="end">{y_fmt.format(y)}</text>')
    for x in LAGS:
        out.append(f'<text x="{sx(x):.1f}" y="{H-B+18}" class="tick" text-anchor="middle">{x:g}</text>')
    out.append(f'<text x="{(L+W-R)/2:.0f}" y="{H-6}" class="axis" text-anchor="middle">feed lag (ms)</text>')
    out.append(f'<text x="14" y="{(T+H-B)/2:.0f}" class="axis" text-anchor="middle" '
               f'transform="rotate(-90 14 {(T+H-B)/2:.0f})">{ylabel}</text>')
    for k, (venue, frac, color, dash) in enumerate(SERIES):
        path = " ".join(f"{'M' if i == 0 else 'L'}{sx(x):.1f},{sy(y):.1f}" for i, (x, y) in enumerate(pts[(venue, frac)]))
        out.append(f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2" stroke-dasharray="{dash}"/>')
        for x, y in pts[(venue, frac)]:
            out.append(f'<circle cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="2.6" fill="{color}"/>')
        ly = T + 14 + k * 20
        out.append(f'<line x1="{W-R+14}" x2="{W-R+38}" y1="{ly}" y2="{ly}" stroke="{color}" stroke-width="2" stroke-dasharray="{dash}"/>'
                   f'<text x="{W-R+44}" y="{ly+4}" class="leg">{venue.title()} · frac {frac}</text>')
    out.append("</svg>")
    return "".join(out)


chart_fill = line_chart("fill_rate", "Quantity fill rate vs market-data delay", "qty fill rate (%)",
                        lambda v: v * 100, "{:.1f}")
chart_rej = line_chart("post_only_rejects", "Post-only rejects vs market-data delay", "rejects (6 cycles)")
chart_mk = line_chart("markout_1s_bps", "1-second markout per fill vs market-data delay", "markout (bps)",
                      y_fmt="{:.2f}")


def sweep_table(venue, frac):
    rows = sorted((r for r in table if r["venue"] == venue and r["queue_position_frac"] == frac),
                  key=lambda r: r["feed_lag_ms"])
    base = rows[0]
    body = []
    for r in rows:
        d = (r["fill_rate"] / base["fill_rate"] - 1) * 100
        dq = (r["fill_qty"] / base["fill_qty"] - 1) * 100
        body.append([
            f"{r['feed_lag_ms']:g}", f"{r['orders_sent']:,}", f(r["fill_qty"], 4),
            f"{dq:+.2f}%", f"{r['fill_rate']*100:.1f}%", f"{d:+.2f}%", f"{r['orders_filled_rate']*100:.1f}%",
            f"{r['through_share']*100:.1f}%", f(r["mean_queue_ahead"], 3),
            f(r["markout_1s_bps"]), f(r["markout_5s_bps"]), f(r["pnl"]), f(r["fees"]),
            f"{r['post_only_rejects']:,}",
        ])
    return tbl(["Lag ms", "Orders", "Filled qty", "Δ filled qty", "Qty fill rate", "Δ fill rate", "Fully filled", "Through",
                "Queue ahead", "MO 1s", "MO 5s", "PnL", "Fees", "PO rej."], body, "small")


new_tests = [
    ("TradeEvent", "legacy trade row reads as canonical trade", "PASS"),
    ("TradeEvent", "normalizers emit canonical trades (Bybit cross sequence)", "PASS"),
    ("TradeEvent", "validation rejects unknown aggressor side", "PASS"),
    ("TradeEvent", "dispatcher routes by type and shares the same instance", "PASS"),
    ("TradeEvent", "recorder writes trades only when asked", "PASS"),
    ("SimulationClock", "timers fire in order, before an event at the same time", "PASS"),
    ("SimulationClock", "time never goes backwards (regressions counted)", "PASS"),
    ("SimulationClock", "cancelled timer does not fire; drain lands the rest", "PASS"),
    ("Queue engine", "semantics: FIFO shared print, zero-size level keeps order, trade-through", "PASS (C++ & Python)"),
    ("Queue engine", "trade and its book delta burn the same queue in either order", "PASS (C++ & Python)"),
    ("Queue engine", "queue_position_frac scales queue ahead at placement", "PASS (C++ & Python)"),
    ("Queue engine", "C++ and Python engines bit-identical, 5,000 random ops × frac 1.0/0.7/0.0", "PASS"),
    ("Maker simulator", "queue position assigned at venue arrival, not decision", "PASS"),
    ("Maker simulator", "strategy inventory changes only when the fill report arrives", "PASS"),
    ("Maker simulator", "order fills while its cancel is in flight", "PASS"),
    ("Maker simulator", "a crossing book never fills without a print", "PASS"),
    ("Maker simulator", "post-only order rejected if the book crossed it before arrival", "PASS"),
    ("Maker simulator", "unchanged quote keeps its queue priority (no cancel/replace)", "PASS"),
    ("Maker simulator", "feed lag takes the queue from the book received after arrival", "PASS"),
    ("Maker simulator", "markouts are signed mid moves after each fill", "PASS"),
    ("Maker simulator", "zero latency when latency is disabled is labelled", "PASS"),
    ("Config", "maker latency = platform model on its own derived seed (OMS draws unchanged)", "PASS"),
    ("Config", "stage overrides support latency sensitivity (300 ns entry)", "PASS"),
    ("Config", "queue / feed-lag / markout settings read from config", "PASS"),
    ("Config", "legacy maker_simulation.latency_ms is rejected", "PASS"),
    ("Replay order", "arrival_ordinal reproduces the live interleaving", "PASS"),
    ("Replay order", "tie priority is a declared per-venue policy", "PASS"),
    ("Replay order", "exchange time breaks receive-time ties before event kind", "PASS"),
    ("Replay order", "duplicate event ids fall back to receive-time ordering", "PASS"),
    ("End-to-end", "live paper and its replay produce identical fills", "PASS"),
]

html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Maker Execution Realism – Improvements Report</title>
<style>
@page {{ size: A4; margin: 16mm 14mm 16mm 14mm; }}
:root {{ --ink:#111827; --muted:#4b5563; --line:#e5e7eb; --soft:#f3f4f6; --accent:#1d4ed8; }}
* {{ box-sizing: border-box; }}
body {{ font-family: -apple-system, "Helvetica Neue", Arial, sans-serif; color: var(--ink); font-size: 10.2pt; line-height: 1.45; margin: 0; background: #fff; }}
h1 {{ font-size: 23pt; margin: 0 0 4px; letter-spacing: -0.3px; }}
h2 {{ font-size: 14.5pt; margin: 22px 0 8px; padding-bottom: 4px; border-bottom: 2px solid var(--accent); }}
h3 {{ font-size: 11.5pt; margin: 14px 0 6px; }}
p {{ margin: 6px 0; }}
.sub {{ color: var(--muted); font-size: 10.5pt; }}
.cover {{ border-left: 5px solid var(--accent); padding: 6px 0 6px 14px; margin-bottom: 14px; }}
.kpis {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin: 12px 0; }}
.kpi {{ border: 1px solid var(--line); border-radius: 6px; padding: 8px 10px; background: var(--soft); }}
.kpi b {{ display: block; font-size: 15pt; }}
.kpi span {{ color: var(--muted); font-size: 8.6pt; }}
table {{ width: 100%; border-collapse: collapse; margin: 6px 0 10px; font-size: 9pt; page-break-inside: auto; }}
tr {{ page-break-inside: avoid; }}
th {{ background: #1f2937; color: #fff; font-weight: 600; padding: 5px 6px; text-align: left; }}
td {{ padding: 4px 6px; border-bottom: 1px solid var(--line); vertical-align: top; }}
tbody tr:nth-child(even) td {{ background: #fafafa; }}
table.small {{ font-size: 8pt; }} table.small td, table.small th {{ padding: 3px 4px; }}
.r {{ text-align: right; }} .l {{ text-align: left; }} .c {{ text-align: center; }}
code {{ font-family: Menlo, Consolas, monospace; font-size: 8.6pt; background: var(--soft); padding: 0 3px; border-radius: 3px; }}
pre {{ font-family: Menlo, Consolas, monospace; font-size: 8.2pt; background: var(--soft); border: 1px solid var(--line); border-radius: 5px; padding: 8px 10px; white-space: pre-wrap; }}
.pill {{ display: inline-block; padding: 0 7px; border-radius: 9px; font-size: 8pt; font-weight: 600; }}
.ok {{ background: #dcfce7; color: #166534; }} .warn {{ background: #fef3c7; color: #92400e; }}
.bad {{ background: #fee2e2; color: #991b1b; }} .info {{ background: #dbeafe; color: #1e40af; }}
.callout {{ border: 1px solid #bfdbfe; background: #eff6ff; border-radius: 6px; padding: 8px 12px; margin: 8px 0; }}
.callout.warn {{ border-color: #fde68a; background: #fffbeb; }}
.chart {{ width: 100%; height: auto; margin: 4px 0 10px; }}
.chart .grid {{ stroke: #e5e7eb; }} .chart .tick {{ font-size: 10px; fill: #4b5563; }}
.chart .axis {{ font-size: 11px; fill: #111827; }} .chart .ct {{ font-size: 13px; font-weight: 600; fill: #111827; }}
.chart .leg {{ font-size: 11px; fill: #111827; }}
.flow {{ font-family: Menlo, Consolas, monospace; font-size: 8.4pt; background: var(--soft); border: 1px solid var(--line); border-radius: 5px; padding: 8px 10px; white-space: pre; line-height: 1.35; }}
.pb {{ page-break-before: always; }}
ul {{ margin: 4px 0 6px 18px; padding: 0; }} li {{ margin: 2px 0; }}
.two {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
</style></head><body>

<div class="cover">
<h1>Maker Execution Realism</h1>
<div class="sub">Improvements report · <b>crypto_MFT_arb_platform</b> (<code>src/crypto_stat_arb</code>) · Revision 2 · 15 October 2026 (supersedes 12 October; incorporates the review of 13.10.2026)</div>
<div class="sub">Scope: queue-aware maker simulation, canonical trade events, latency/clock unification, C++ matching-engine integration, verification and sensitivity study.</div>
</div>

<div class="kpis">
<div class="kpi"><b>252 / 252</b><span>crypto_stat_arb tests passing (baseline 199)</span></div>
<div class="kpi"><b>Bit-identical</b><span>C++ vs Python engine on 230,808 real events</span></div>
<div class="kpi"><b>Identical</b><span>live paper fills vs replay of the same session</span></div>
<div class="kpi"><b>84 runs</b><span>sensitivity sweep, 6 cycles, 0 sanity violations</span></div>
</div>

<h2>1. Executive summary</h2>
<p>The maker (passive, post-only) simulation previously ran in a separate “universe” from the rest of the platform: its own flat 10&nbsp;ms latency, wall-clock timing, raw-dictionary trades and a Python fallback model, while the C++ matching engine had never been compiled or exercised. This session unified it with the platform’s existing abstractions and verified the result end to end.</p>
<ul>
<li><b>One execution path:</b> canonical <code>MarketEvent</code> + new <code>TradeEvent</code> → one event clock (<code>SimulationClock</code>) → platform <code>LatencyModel</code> → venue arrival → C++ <code>mm::MatchingEngine</code> → print-driven fills → fill-report latency → strategy.</li>
<li><b>Queue position at venue arrival</b> (decision + strategy + risk + entry latency), cancel races, post-only rejects, delayed inventory knowledge and preserved queue priority are now modelled.</li>
<li><b>C++ engine integrated and corrected</b> (three queue-accounting bugs fixed), with a bit-identical Python mirror and parity tests.</li>
<li><b>Sensitivity:</b> adding 35&nbsp;ms of market-data staleness reduces <b>filled quantity</b> by {abs(D[("BINANCE",35)]["qty"]):.2f}% (Binance) / {abs(D[("BYBIT",35)]["qty"]):.2f}% (Bybit) and 75&nbsp;ms by {abs(D[("BINANCE",75)]["qty"]):.2f}% / {abs(D[("BYBIT",75)]["qty"]):.2f}%, while the 1-second markout per fill moves by at most {max(abs(D[(v,l)]["mk"]) for v in ("BINANCE","BYBIT") for l in (35,75)):.3f}&nbsp;bps. The results are <b>not</b> produced by stale-book optimism.</li>
<li><b>Economic finding:</b> at-touch passive quoting loses ~1&nbsp;bps per fill to adverse selection plus a 2&nbsp;bps maker fee — negative in all 28 aggregated scenarios.</li>
</ul>

<h2>Revision 2 – review of 13.10.2026 and corrections</h2>
<p>This revision answers the trader's <i>Maker Execution – Initial review (13.10.2026)</i>. The first issue was traced to deleted recorded files (removed to free disk space); the others were errors in our reporting or audit messages and are corrected here regardless of the files.</p>
{tbl(["#", "Review finding", "Cause", "Correction"], [
 ["1", "A narrow PASS is presented next to a failing full session audit", "Recorded <code>raw.jsonl</code> (15 parts), <code>wire.jsonl</code> (6 parts) and <code>paper/canonical_events.jsonl</code> (5 cycles) had been deleted; the audit then failed on storage checks and crashed in backtest replay. Our evidence reported only the simulation component. The previous report also misdescribed that run's backtest_replay failure as replay-vs-paper DIVERGED; it was a <code>FileNotFoundError</code>.",
  "Files restored where they could be derived exactly (§ R.1). The audit now reports absent inputs as <code>INPUT_MISSING</code> instead of FAIL/crash, and the evidence bundle states each decision's scope next to the full audit's overall status (§ R.2)."],
 ["2", "Engine and ordering diagnostics contradict their metrics", "Audit checks printed their failure text even when they passed (13 checks).", "Every check now has a success message; a test asserts no OK row carries failure text. The C++ engine was in use, as the metric said."],
 ["3", "Filled quantity and quantity fill rate were confused", "The previous report quoted fill-rate changes as filled-quantity changes.",
  f"Both are now shown separately (§ 6). At 35&nbsp;ms filled quantity changes {D[('BINANCE',35)]['qty']:+.2f}% / {D[('BYBIT',35)]['qty']:+.2f}% (fill rate {D[('BINANCE',35)]['rate']:+.2f}% / {D[('BYBIT',35)]['rate']:+.2f}%); at 75&nbsp;ms {D[('BINANCE',75)]['qty']:+.2f}% / {D[('BYBIT',75)]['qty']:+.2f}%. The 35&nbsp;ms acceptance criterion still passes."],
 ["4", "Portability and deployment readiness not established", "The review received outputs only; the compiled module is arm64 / CPython 3.12 and is not built in CI.", "Source, tests, CMake files, configs and <code>uv.lock</code> are in github.com/vaibhaw1994kumar/CryptoMFT. Build environment: macOS 15 (Darwin 24.3) arm64, CPython 3.12, pybind11 3.1.0, CMake. CI build of the extension is the next step (§ 8)."],
], align=["c","l","l","l"])}
<h3>R.1 Data restoration</h3>
{tbl(["Item", "Result"], [
 ["<code>paper/canonical_events.jsonl</code> (cycles 0006–0010)", "Rebuilt by concatenating the parts' <code>canonical.jsonl</code>; all 5 hash-equal to the <code>events_sha256</code> the original audits recorded (method verified byte-identical on 3 intact cycles first)."],
 ["<code>raw.jsonl</code> from <code>wire.jsonl</code> (<code>scripts/rebuild_raw_from_wire.py</code>)", "Method reproduced 4 intact parts byte for byte; 9 missing files restored, each matching the SHA-256 and record count written live in <code>raw_manifest.json</code>."],
 ["Unrecoverable (no <code>wire.jsonl</code>)", ", ".join(f"<code>{p}</code>" for p in integrity["lineage_incomplete"]) + " – need Trash/backup; their canonical, trades and trace files are intact, so replays are unaffected."],
 ["Integrity now", f"{integrity['lineage_complete']}/{integrity['parts']} parts with complete, checksum-exact lineage; paper event files missing: {len(integrity['paper_events_missing'])}; checksum mismatches: {len(integrity['raw_present_but_mismatched']) + len(integrity['paper_events_mismatched'])}."],
], align=["l","l"])}
<h3>R.2 Session audit, cycle-0006 (after restoration)</h3>
{tbl(["Component", "Status"], [[name, pill(status, "ok" if status == "OK" else "warn" if status in ("WARN", "INPUT_MISSING") else "bad")] for name, status in audit["new"]["components"].items()], "small", ["l","l"])}
<p class="sub">Overall: <b>{audit["full_audit_overall"]}</b>. Issues at INPUT_MISSING or FAIL: {"; ".join(f'{i["component"]}.{i["check"]} = {i["status"]}' for i in audit["full_audit_issues"]) or "none"}. The maker decision in this report covers the <i>simulation</i> component only ({audit["new"]["simulation"]["status"]}); it does not imply the full audit passed.</p>

<h2>2. Problems found at the start</h2>
{tbl(["#", "Problem", "Impact"], [
 ["1", "<code>maker_sim.py</code> used <code>latency_ms=10</code> independently of the platform <code>LatencyModel</code> (OMS used the <code>singapore</code> preset).", "Maker and OMS simulations ran on different latency assumptions; Binance entry is ~39&nbsp;ms in the preset."],
 ["2", "Queue position was taken when the order was <i>decided</i>, not when it reached the venue.", "Optimistic queue placement; no causal link to entry latency."],
 ["3", "Trades were loaded as raw dicts outside the canonical pipeline; no <code>TradeEvent</code> type.", "No validation, no shared envelope, special JSON only <code>maker_sim</code> understood."],
 ["4", "Replay forced “book before trade” on equal receive timestamps.", "Invented a universal exchange rule; could change queue states."],
 ["5", "Live paper drove the maker with processing time, replay with receive time; vendored code monkey-patched <code>time.monotonic</code>.", "Live and replay timelines guaranteed to diverge."],
 ["6", "All 44 audited cycles ran <code>python_queue_model</code>; the C++ engine was never built for Python 3.12.", "C++ path never exercised in this project."],
 ["7", "Vendored <code>DryRunOrderManager</code> C++ path: book never fed to the engine; <code>uint64</code> fill ids looked up against string order ids; crashed (<code>'_CppFillEngine' has no attribute 'engine'</code>) once importable.", "Every C++ trade-driven fill would have been dropped."],
 ["8", "Random “price-touch decay” fills and a 35% adverse-selection gate.", "Fills without any print behind them; not traceable."],
 ["9", "Quotes cancelled and re-placed every second even at an unchanged price.", "Queue priority discarded every interval."],
 ["10", "C++ queue accounting: size-0 level erased our resting orders; shared-level FIFO volume wrongly consumed; trade + book delta could double count.", "Lost orders, stuck queues, optimistic/pessimistic drift."],
], align=["c", "l", "l"])}

<h2>3. Improvements implemented</h2>
<h3>3.1 P0 – Core architecture</h3>
{tbl(["Gap", "What was done"], [
 ["Latency parity", "Maker builds its latency from the config <code>latency</code> section (same preset/overrides as the OMS) as a <b>separate instance with a derived seed</b>, so OMS draws are unchanged. Stage composition mirrors the OMS: decision <i>t</i> → dispatch <i>t</i>+strategy+risk → arrival +entry; cancel applies +cancel; fill reports +fill; acks/rejects +submit_ack. <code>maker_simulation.latency_ms</code> now raises an error; <code>maker_simulation.latency</code> overrides individual stages (e.g. 300&nbsp;ns entry)."],
 ["Queue at arrival", "Orders join the C++ queue at venue arrival against the book at that instant. Cancels apply after cancel latency and the order can still fill meanwhile. Post-only orders crossing at arrival are rejected. Strategy inventory changes only when the fill report is observed; the inventory band uses that view plus all unfinished orders."],
 ["Canonical trades", "New <code>models.TradeEvent</code> (sibling of <code>MarketEvent</code>, schema 1.1.0, reads legacy rows) and <code>EventType.TRADE</code>. <code>canonical.py</code> validates and dispatches both types with type-filtered sinks. Normalizers emit <code>TradeEvent</code> (<code>TradeTick</code> kept as alias). Live feed validates every trade envelope."],
 ["One event clock", "New <code>sim_clock.SimulationClock</code>: <code>schedule</code>, <code>advance_to</code>, <code>drain</code>; deterministic (ts, sequence) order; timers due at an event fire before it; monotone time with regression counter. Live paper now uses each event’s <code>receive_ts_ns</code>."],
], align=["l", "l"])}
<h3>3.2 P1 – Ordering, fills, cadence</h3>
{tbl(["Gap", "What was done"], [
 ["Native ordering", "Live feed stamps one <code>arrival_ordinal</code> across book and trade streams (written to <code>trace.jsonl</code> and <code>TradeEvent</code>). Replay reproduces the live interleaving from the trace (older traces: line order). Fallback key: receive time → exchange time → declared per-venue kind priority (<code>replay_tie_priority</code>) → event id. Native book/trade sequences are never compared. Duplicate ids fail closed to the fallback."],
 ["Print-driven fills", "<code>maker_engine.py</code>: <code>CppQueueEngine</code> wraps the compiled engine with an explicit id map; <code>PythonQueueEngine</code> mirrors the arithmetic exactly. Every fill row records the causing <code>trade_event_id</code>, queue ahead at arrival and decision/dispatch/arrival/match/report timestamps. Book crossings without a print are counted (<code>crossed_without_print</code>), never filled."],
 ["Cadence vs latency", "<code>quote_interval_ms</code> is a clock timer (strategy scheduling). Unchanged quotes keep resting and keep queue priority; moved quotes are cancelled and replaced."],
], align=["l", "l"])}
<h3>3.3 C++ matching engine (<code>sim_engine/include/mm/matching_engine.hpp</code>)</h3>
<ul>
<li>Compiled for arm64 / Python 3.12 (pybind11 3.1.0) into <code>sim_engine/</code>; now the default (<code>engine="auto"</code> → <code>cpp_mm_matching_engine</code>).</li>
<li>Level sizes come only from the book feed; trades only burn queue ahead.</li>
<li>Several own orders at one level share a print: only our own fills absorb volume.</li>
<li>Book updates can only cap queue ahead; the capped amount is a <code>clamp_credit</code> so a print and its book delta count once in either arrival order.</li>
<li>Print strictly through our price → full fill at our price. Size-0 level keeps our resting orders.</li>
<li>New <code>set_queue_position_frac</code> (0–1, default 1.0 = back of visible queue).</li>
</ul>
<h3>3.4 Calibration and analysis features</h3>
<ul>
<li><code>queue_position_frac</code> – share of the visible level ahead of a new order (old Python model hard-coded 0.7).</li>
<li><code>feed_lag_ms</code> – market-data staleness; venue instants are placed later on the receive-time clock.</li>
<li>Per-fill markouts (<code>markout_horizons_ms</code>, default 1&nbsp;s, 5&nbsp;s) as an adverse-selection measure.</li>
<li><code>scripts/maker_sensitivity.py</code> – parallel sweep with CSV/JSON/Markdown output and <code>--from-csv</code> re-aggregation.</li>
<li>Report schema 2.0.0 adds quotes kept, post-only rejects, cancel races, queue ahead, order states, latency percentiles; replay rejects invalid envelopes with counts; session audit adds latency source and an arrival-order check.</li>
</ul>

<h3>3.5 Files changed</h3>
{tbl(["File", "Change"], [
 ["<code>maker_sim.py</code>", "Rewritten (872 lines): event-clock simulator, OMS-parity latency, replay ordering, sweep helpers"],
 ["<code>maker_engine.py</code>", "New (248): C++ wrapper + bit-identical Python mirror"],
 ["<code>sim_clock.py</code>", "New (122): shared event-time clock"],
 ["<code>models.py</code>", "<code>TradeEvent</code>, <code>EventType.TRADE</code>, JSON support"],
 ["<code>canonical.py</code>", "Validation and type-aware dispatch for books and trades"],
 ["<code>market_data_output.py</code>", "Normalizers emit <code>TradeEvent</code>; <code>TradeTick</code> alias"],
 ["<code>live_feed.py</code>", "Shared <code>arrival_ordinal</code>; trace rows; trade envelope validation"],
 ["<code>paper_live.py</code>", "Maker driven by event receive time; <code>finish()</code>; fills JSONL"],
 ["<code>session_audit.py</code>", "Latency source metric; replay-order check; message update"],
 ["<code>sim_engine/…/matching_engine.hpp</code>, <code>bindings.cpp</code>", "Queue-accounting fixes, <code>clamp_credit</code>, <code>queue_position_frac</code>"],
 ["<code>tests/test_maker_execution_path.py</code>", "New: 30 tests (+ subtests)"],
 ["<code>tests/test_watch_pipeline.py</code>", "Fixture writes canonical trades"],
 ["<code>scripts/maker_sensitivity.py</code>", "New: sensitivity sweep"],
 ["<code>config/paper_watch.json</code>", "Removed obsolete <code>latency_ms</code>"],
 ["<code>docs/execution_realism.md</code>, <code>.gitignore</code>s", "Maker-simulation documentation, build steps; compiled module ignored"],
], align=["l", "l"])}
<p class="sub">Package diff (<code>src/crypto_stat_arb</code>): 9 files changed, 1,014 insertions, 287 deletions, plus 2 new modules. All changes are committed to github.com/vaibhaw1994kumar/CryptoMFT (branch <code>main</code>).</p>

<h2 class="pb">4. Architecture after the changes</h2>
<div class="flow">  Venue adapters (Binance, Bybit)                      one arrival_ordinal across streams
        │                                              (trace.jsonl, TradeEvent)
        ├── MarketEvent (book)      TradeEvent (print)
        └──────────────┬───────────────┘
                       ▼
             CanonicalEventDispatcher  ── validate once, same instance to every sink
                       ▼
              SimulationClock (receive_ts_ns)  ── timers: quote cadence, arrivals,
                       │                           cancels, acks, fill reports, markouts
                       ▼
       Strategy decision ─ +strategy ─ +risk ─ dispatch      LatencyModel (config preset,
                       │                                       derived seed, overrides)
                       ▼ +entry (+feed_lag)
             VENUE ARRIVAL → queue ahead = frac × visible level
                       ▼
           C++ mm::MatchingEngine  (Python mirror if not built)
           FIFO · print-driven · trade-through · clamp credit
                       ▼
             Fill (trade_event_id) ── +fill latency ──► strategy inventory view
                       ▼
          Report: fills JSONL, latency p50/p95/p99, markouts, audit</div>

<h2>5. Test and verification results</h2>
<h3>5.1 Test suites</h3>
{tbl(["Suite / check", "Result", "Notes"], [
 ["crypto_stat_arb tests – baseline (before changes)", pill("199 passed", "ok"), "Python 3.12, 11.8 s"],
 ["crypto_stat_arb tests – after core changes", pill("225 passed", "ok"), "+26 new tests"],
 ["crypto_stat_arb tests – maker work", pill("229 passed", "ok"), "9 subtests passed, 12.4 s"],
 ["crypto_stat_arb tests – now (incl. residual, inventory, audit-input tests)", pill("252 passed", "ok"), "9 subtests passed"],
 ["New module <code>test_maker_execution_path.py</code>", pill("30 passed", "ok"), "listed in 5.2"],
 ["platform_kit (trading_platform_poc) – before vs after", pill("698 passed / 13 failed — identical", "warn"), "Failures pre-existing: missing <code>uv</code>, <code>tardis_dev</code>, <code>pyarrow</code>, <code>hftbacktest</code> on this host; 12 collection errors in both runs"],
 ["Offline acceptance gate <code>scripts/run_all_local.py</code> – maker work", pill("PASSED", "ok"), "225 tests, benchmark PASS, paper release id <code>bfbcab66…</code> unchanged (deterministic)"],
 ["Offline acceptance gate – latest (12 Oct)", pill("PASSED", "ok"), "250 tests, benchmark PASS, release id <code>402f70d9…</code> (changed by the pre-funded inventory config)"],
 ["C++ engine build (cmake + pybind11 3.1.0)", pill("Built, 0 errors", "ok"), "<code>mm_matching_engine.cpython-312-darwin.so</code>"],
 ["Pre-change failures exposed by the build", pill("2 failed → fixed", "info"), "Vendored C++ path crashed once importable; resolved by moving off <code>DryRunOrderManager</code>"],
], align=["l", "l", "l"])}

<h3>5.2 New tests (all passing)</h3>
{tbl(["Area", "Test", "Result"], [[a, t, pill(r, "ok")] for a, t, r in new_tests], "small", ["l", "l", "l"])}

<h3>5.3 Verification on real recorded data</h3>
{tbl(["Check", "Data", "Result"], [
 ["C++ vs Python engine parity", "cycle-0006: 79,365 book events + 151,443 trades", pill("fills JSONL byte-identical", "ok")],
 ["C++ queue semantics smoke test", "trade-first vs book-first; two orders one level; size-0 level; trade-through", pill("all as designed", "ok")],
 ["Live paper vs replay parity", "fake Binance + Bybit books and trades through <code>LivePaperSession</code>", pill("identical fill lists", "ok")],
 ["Replay ordering on real sessions", "trace-based <code>arrival_ordinal</code>", pill("arrival_ordinal used", "ok")],
 ["Duplicate event ids in traces", "all 46 recorded cycles", pill("0 duplicates", "ok")],
 ["Session audit <code>analyze-session</code> – simulation component", "cycle-0006", pill("simulation: OK (was WARN)", "ok")],
 ["Session audit – full, after restoration", "cycle-0006", pill(f"overall {audit['full_audit_overall']}", "warn")],
 ["Recorded inputs (presence, checksums)", f"{integrity['parts']} parts, {integrity['paper_event_files']} paper runs", pill(f"{integrity['lineage_complete']}/{integrity['parts']} complete lineage", "warn" if integrity['lineage_incomplete'] else "ok")],
 ["CLI <code>simulate-maker</code>", "cycle-0001", pill("C++ engine, 0 rejected envelopes, 0 sanity violations", "ok")],
 ["Sensitivity sweep", "84 runs × 6 cycles", pill("0 sanity violations", "ok")],
], align=["l", "l", "l"])}
<div class="callout warn"><b>Correction.</b> The previous revision said the cycle-0006 audit's <code>backtest_replay: FAIL</code> was the replay-vs-paper divergence. In that run it was a <code>FileNotFoundError</code>: <code>paper/canonical_events.jsonl</code> had been deleted. With the file restored (hash-verified), backtest replay runs; its trading metrics are within tolerance and only the strategy/risk latency percentiles differ, now reported as a separate latency-calibration WARN.</div>

<h3>5.4 Before vs after on cycle-0006 (same recorded data)</h3>
{tbl(["Metric", "Before – Binance", "After – Binance", "Before – Bybit", "After – Bybit"], [
 ["Engine", "python_queue_model", "cpp_mm_matching_engine", "python_queue_model", "cpp_mm_matching_engine"],
 ["Latency", "flat 10 ms", "singapore preset (entry p50 39.1 ms)", "flat 10 ms", "singapore preset (entry p50 5.6 ms)"],
 ["Orders sent (quotes)", "2,739", "1,132 (+1,787 kept resting)", "3,088", "1,513 (+1,625 kept)"],
 ["Fill events", "870", "1,130 (378 through)", "823", "870 (504 through)"],
 ["Post-only rejects", "–", "32", "–", "6"],
 ["Fills while cancel pending", "–", "9", "–", "1"],
 ["Mean queue ahead at arrival", "–", "3.576", "–", "0.504"],
 ["Sanity violations", "0", "0", "0", "0"],
], align=["l", "r", "r", "r", "r"])}
<p class="sub">Old and new numbers are not directly comparable: “quotes” now means orders actually sent, the old model used a 0.7 queue fraction plus random touch fills, and unchanged quotes are no longer cancelled/replaced.</p>

<h2 class="pb">6. Sensitivity study: market-data delay × queue position</h2>
<p><b>Question:</b> how much of the simulated maker outcome survives when we remove the assumption that the observed book equals the venue book at order arrival?</p>
<p><b>Method:</b> {meta['runs']} deterministic replays on the C++ engine, ordering <code>{meta['ordering']}</code>, cycles {", ".join(c[6:10] for c in meta['cycles'])} (≈30 min of live Binance + Bybit data each, 14–15 Sep 2026). Swept <code>feed_lag_ms</code> ∈ {{{", ".join(f"{x:g}" for x in LAGS)}}} and <code>queue_position_frac</code> ∈ {{1.0, 0.7}}. Quantity fill rate = filled qty / (orders sent × 0.001 BTC). Markouts: signed mid move after each fill (positive = favourable). PnL is mark-to-market incl. fees and inventory drift.</p>
{chart_fill}
<div class="two">{chart_rej}{chart_mk}</div>

<h3>Findings</h3>
{tbl(["Finding", "Evidence"], [
 ["Delay costs some fills, smoothly – no cliff", f"Filled quantity {D[('BINANCE',35)]['qty']:+.2f}% Binance / {D[('BYBIT',35)]['qty']:+.2f}% Bybit at 35 ms; {D[('BINANCE',75)]['qty']:+.2f}% / {D[('BYBIT',75)]['qty']:+.2f}% at 75 ms (fill rate {D[('BINANCE',35)]['rate']:+.2f}% / {D[('BYBIT',35)]['rate']:+.2f}% and {D[('BINANCE',75)]['rate']:+.2f}% / {D[('BYBIT',75)]['rate']:+.2f}%; frac 1.0)"],
 ["Per-fill economics unchanged → results not driven by stale-book optimism", "1 s markout −0.94 → −0.96 bps (Binance), −1.01 → −1.01 (Bybit) from 0 to 75 ms"],
 ["Queue position matters little for at-touch quotes", "frac 0.7 vs 1.0 changes filled qty ~1%; 38% (Binance) and 57% (Bybit) of fills are trade-throughs"],
 ["Post-only rejects are the strongest delay signature", "Binance 157 → 388, Bybit 55 → 409 from 0 to 75 ms — best calibration target vs real testnet reject rate"],
 ["At-touch passive harness is negative in every scenario", "≈ −1 bps adverse selection per fill + 2 bps maker fee; fees ≈ 58% of the loss"],
], align=["l", "l"])}
<div class="callout"><b>Proposed acceptance criterion</b> (September data, cycles 0006–0010 and 0040; to be repeated across other regimes before it becomes a platform-wide invariant): at 35&nbsp;ms market-data delay, filled quantity falls by ≤ 5% and the 1-second markout per fill shifts by ≤ 0.1&nbsp;bps. Both venues pass: filled quantity {D[("BINANCE",35)]["qty"]:+.2f}% / {D[("BYBIT",35)]["qty"]:+.2f}%, markout shift {D[("BINANCE",35)]["mk"]:+.4f} / {D[("BYBIT",35)]["mk"]:+.4f}&nbsp;bps.</div>

<h3>Binance – queue_position_frac 1.0</h3>{sweep_table("BINANCE", 1.0)}
<h3>Binance – queue_position_frac 0.7</h3>{sweep_table("BINANCE", 0.7)}
<h3>Bybit – queue_position_frac 1.0</h3>{sweep_table("BYBIT", 1.0)}
<h3>Bybit – queue_position_frac 0.7</h3>{sweep_table("BYBIT", 0.7)}
<p class="sub">Columns: Δ filled qty = filled quantity vs the 0 ms row; Qty fill rate = filled qty ÷ offered qty (orders sent × 0.001 BTC); Δ fill rate = its change vs 0 ms (the two differ because orders sent vary with lag); Fully filled = orders filled completely ÷ orders sent; Through = share of fill events from prints beyond our price; MO = markout (bps); PO rej. = post-only rejects. Totals over 6 cycles.</p>
<div class="callout warn">An earlier version of this table counted fill <i>events</i>, which made frac 0.7 look worse; it fills the same or slightly more quantity in fewer, larger pieces. The tables above use quantity.</div>

<h2>7. Known limitations</h2>
<ul>
<li><b>Queue calibration (P2) not done</b> – needs real order outcomes (testnet post-only orders).</li>
<li>Default <code>feed_lag_ms = 0</code> keeps OMS parity; the sweep quantifies the bias rather than removing it.</li>
<li>Book envelope does not carry <code>arrival_ordinal</code> (its JSON schema rejects extra fields); replay joins it from <code>trace.jsonl</code>.</li>
<li>Maker report schema 2.0.0: “quotes” = orders sent; old and new cycle totals in <code>AUDIT_REPORT.md</code> are not comparable.</li>
<li>Compiled module is arm64 / Python 3.12 and git-ignored; Docker/CI fall back to the Python mirror until a build step is added.</li>
<li>FAST_MM’s own copy of the C++ engine still has the queue bugs; vendored <code>dry_run_core.py</code>/<code>sim_fill_engine.py</code> are now unused.</li>
<li>Sweep covers one market regime (14–15 Sep 2026); PnL includes inventory drift.</li>
</ul>

<h2>8. Recommended next steps</h2>
{tbl(["Priority", "Item"], [
 ["P0", "Build the C++ engine in Docker/CI; release path runs <code>engine=cpp</code> with a C++↔Python parity gate"],
 ["P1", "Move the matching engine to one shared component used by FAST_MM and crypto_stat_arb (port fixes once)"],
 ["P1", "Testnet calibration dataset: 100–500+ small post-only orders; fit queue survival and feed lag (post-only reject rate)"],
 ["P2", "Explicit report metric names (<code>orders_sent</code>, <code>orders_retained</code>) and legacy-schema flag in audits"],
 ["P2", "Extend the sweep to more regimes and add it as a permanent acceptance suite"],
 ["P3", "Archive unused vendored <code>dry_run_core.py</code> / <code>sim_fill_engine.py</code>"],
], align=["c", "l"])}

<h2>Appendix – reproduce</h2>
<pre>cd crypto_MFT_arb_platform
# build the C++ engine
python3.12 -m pip install pybind11
cmake -S src/crypto_stat_arb/sim_engine -B build/sim_engine -Dpybind11_DIR="$(python3.12 -m pybind11 --cmakedir)"
cmake --build build/sim_engine
# tests and acceptance
PYTHONPATH=src python3.12 -m pytest tests --ignore=tests/platform_kit -q
PYTHONPATH=src python3.12 scripts/run_all_local.py
# maker replay of a recorded cycle and the sensitivity sweep
PYTHONPATH=src python3.12 -m crypto_stat_arb.cli simulate-maker --config config/paper_watch.json \\
    --session-dir runs/paper_watch/live/cycle-0006-20260914T142731Z --output runs/maker.json
python3.12 scripts/maker_sensitivity.py --top 6 --workers 6
# restore raw.jsonl from wire.jsonl (keeps only checksum-verified files) and rebuild the evidence + this report
python3.12 scripts/rebuild_raw_from_wire.py --verify-intact runs/paper_watch/live --limit 4
python3.12 scripts/rebuild_raw_from_wire.py --restore runs/paper_watch/live
python3.12 scripts/collect_maker_evidence.py
python3.12 scripts/build_maker_report.py</pre>
<p class="sub">Sweep artifacts: <code>runs/maker_sensitivity/20261011T172155Z/</code> (results.csv, summary.json, SENSITIVITY.md).</p>
</body></html>"""

HTML.write_text(html, encoding="utf-8")
pdf = HTML.with_suffix(".pdf")
subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                f"--print-to-pdf={pdf}", HTML.as_uri()], capture_output=True, timeout=120)
print(pdf, pdf.stat().st_size)
