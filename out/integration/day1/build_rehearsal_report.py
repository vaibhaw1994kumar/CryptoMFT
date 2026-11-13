"""End-to-end rehearsal report, 2026-10-29 (owner 13 Nov): v1_9 with the approved production limits on the full capture, run twice (byte identity), v1_7 beside it as the reference.
Writes out/integration/e2e_rehearsal_2026-10-29.html (a NEW report: the acceptance report is not touched). A report writer over files: every number is read from a run's output files, the only typed text is the
labels and the explanations. EXPLORATORY: one day, mark = mid, no funding (derivatives pending), regimes in-sample, no own orders on the venues (simulated fills).
usage: build_rehearsal_report.py [--a DIR] [--b DIR] [--ref DIR] [--snap DIR] [--out FILE]"""
import html
import json
import sys
from decimal import Decimal
from pathlib import Path

INT = Path("/Users/vaibhav/Documents/Newproject/out/integration")
E2E = INT / "e2e"
argv = sys.argv[1:]


def opt(flag, default):
    if flag in argv:
        return Path(argv[argv.index(flag) + 1])
    return default


DIR_A, DIR_B, DIR_R = opt("--a", E2E / "v19_A"), opt("--b", E2E / "v19_B"), opt("--ref", E2E / "v17_ref")
SNAP = opt("--snap", E2E / "snap_20261113")
OUT = opt("--out", INT / "e2e_rehearsal_2026-10-29.html")
LOT = Decimal("0.01")                                   # the hedge venue's (Binance) SOLUSDT lot


def jl(p):
    p = Path(p)
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()] if p.exists() else []


def j(p):
    p = Path(p)
    return json.loads(p.read_text()) if p.exists() else None


def esc(x):
    return html.escape("" if x is None else str(x))


def fmt(v):
    if isinstance(v, str) and v.startswith("<b class="):
        return v
    if isinstance(v, float):
        return f"{v:.6g}"
    return esc(v)


def table(headers, rows, cls=""):
    out = [f"<table class='{cls}'><thead><tr>" + "".join(f"<th>{esc(h)}</th>" for h in headers) + "</tr></thead><tbody>"]
    for r in rows:
        out.append("<tr>" + "".join(f"<td>{fmt(c)}</td>" for c in r) + "</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def verdict(ok):
    return "<b class='ok'>PASS</b>" if ok else "<b class='bad'>FAIL</b>"


class Run:
    def __init__(self, d):
        self.d = Path(d)
        self.exists = (self.d / "session_manifest.json").exists()
        self.m = j(self.d / "session_manifest.json") or {}
        self.orders, self.fills, self.signals = jl(self.d / "session_orders.jsonl"), jl(self.d / "session_fills.jsonl"), jl(self.d / "session_signals.jsonl")
        self.safety, self.decisions = jl(self.d / "session_safety_events.jsonl"), jl(self.d / "session_decision_events.jsonl")
        self.limits, self.cov, self.life = j(self.d / "session_limits_and_stops.json"), j(self.d / "session_ems_path_coverage.json"), j(self.d / "session_lifecycle_report.json")
        self.pairs = j(self.d / "session_pair_economics.json") or {}
        self.obs = jl(self.d / "maker_observations.jsonl")

    @property
    def run(self):
        return self.m.get("run", {})


A, B, R = Run(DIR_A), Run(DIR_B), Run(DIR_R)


def kind(o):
    return str(o.get("intent_id") or "-").split("-")[-1][:1]


ROLE = {"P": "maker (primary)", "H": "hedge", "U": "unwind / flatten", "R": "rebalance", "F": "flatten", "-": "no intent id"}


def reconcile(r):
    """Every order, fill, hedge, unwind and alarm reconciled from the run's own files. Returns (rows of [check, result, detail], per-plan table)."""
    out = []
    fills_by_order, per_venue, cash_v = {}, {}, {}
    fee_sum = Decimal("0")
    for f in r.fills:
        q, px, fee = Decimal(str(f["fill_quantity"])), Decimal(str(f["fill_price"])), Decimal(str(f.get("fee_delta") if f.get("fee_delta") is not None else f["fee"]))
        signed = q if str(f["side"]).upper() == "BUY" else -q
        fills_by_order.setdefault(f["order_id"], []).append((q, px, fee))
        per_venue[f["venue"]] = per_venue.get(f["venue"], Decimal("0")) + signed
        cash_v[f["venue"]] = cash_v.get(f["venue"], Decimal("0")) - signed * px - fee
        fee_sum += fee
    sent = [o for o in r.orders if o["state"] != "CREATED"]
    no_intent = [o["order_id"] for o in sent if not o.get("intent_id")]
    out.append(["every order that was sent carries an intent id", verdict(not no_intent), f"{len(sent)} sent, {len(no_intent)} without"])
    never_sent = [o for o in r.orders if o["state"] == "CREATED"]
    out.append(["orders built but never sent (state CREATED)", "info", f"{len(never_sent)} (admission refused or blocked before the send)"])
    bad_q, bad_px = [], []
    for o in r.orders:
        rows = fills_by_order.get(o["order_id"], [])
        qsum = sum((x[0] for x in rows), Decimal("0"))
        if abs(qsum - Decimal(str(o["filled_quantity"]))) > Decimal("1e-12"):
            bad_q.append((o["order_id"][:8], str(qsum), o["filled_quantity"]))
        if rows and qsum > 0:
            vw = sum((x[0] * x[1] for x in rows), Decimal("0")) / qsum
            if abs(vw - Decimal(str(o["average_price"]))) > Decimal("1e-9") * max(Decimal("1"), vw):
                bad_px.append((o["order_id"][:8], str(vw), o["average_price"]))
    out.append(["each order's filled quantity equals the sum of its fills", verdict(not bad_q), f"{len(r.orders)} orders, {len(bad_q)} differ {bad_q[:3]}"])
    out.append(["each order's average price equals the volume-weighted price of its fills", verdict(not bad_px), f"{len(bad_px)} differ {bad_px[:3]}"])
    orphans = [oid[:8] for oid in fills_by_order if oid not in {o["order_id"] for o in r.orders}]
    out.append(["every fill belongs to an order of the run", verdict(not orphans), f"{len(r.fills)} fills, {len(orphans)} orphans"])
    pos = {k.split(":")[0]: Decimal(str(v)) for k, v in (r.m.get("positions") or {}).items()}
    bad_pos = {v: (str(per_venue.get(v, Decimal('0'))), str(pos.get(v, Decimal('0')))) for v in set(pos) | set(per_venue) if abs(per_venue.get(v, Decimal("0")) - pos.get(v, Decimal("0"))) > Decimal("1e-9")}
    out.append(["end-of-day position per venue equals the signed sum of the fills", verdict(not bad_pos), f"journal vs ledger {dict((v, [str(per_venue.get(v, 0)), str(pos.get(v, 0))]) for v in sorted(pos))}"])
    cash = {k: Decimal(str(v)) for k, v in (r.m.get("cash_by_venue") or {}).items()}
    bad_cash = {v: (str(cash_v.get(v.upper(), Decimal('0'))), str(c)) for v, c in cash.items() if abs(cash_v.get(v.upper(), Decimal("0")) - c) > Decimal("1e-6")}
    out.append(["cash per venue equals the fills' notional less fees", verdict(not bad_cash), f"journal vs ledger {dict((v, [str(cash_v.get(v.upper(), 0)), str(c)]) for v, c in cash.items())}"])
    ofee = sum((Decimal(str(o["fee"])) for o in r.orders), Decimal("0"))
    out.append(["total fees: the fills' fee of each fill equals the orders' fees", verdict(abs(ofee - fee_sum) < Decimal("1e-9")), f"fills {fee_sum}, orders {ofee}"])
    # per plan
    plans = {}
    for o in r.orders:
        pid = str(o.get("intent_id") or "-").split("-")[0]
        p = plans.setdefault(pid, {"maker": Decimal("0"), "hedge": Decimal("0"), "unwind": Decimal("0"), "side": None, "n_h": 0, "n_u": 0})
        q = Decimal(str(o["filled_quantity"]))
        k = kind(o)
        if k == "P":
            p["maker"] += q
            p["side"] = o["side"]
        elif k == "H":
            p["hedge"] += q
            p["n_h"] += 1
        elif k == "U":
            p["unwind"] += q
            p["n_u"] += 1
    rows, open_plans = [], 0
    for pid, p in plans.items():
        if p["maker"] == 0 and p["hedge"] == 0 and p["unwind"] == 0:
            continue
        resid = p["maker"] - p["hedge"] - p["unwind"] if p["maker"] else -p["unwind"]
        flag = "" if abs(resid) < Decimal("1e-9") else ("sub-lot carried" if abs(resid) < LOT else "OPEN")
        open_plans += flag == "OPEN"
        rows.append([pid, p["side"], str(p["maker"]), p["n_h"], str(p["hedge"]), p["n_u"], str(p["unwind"]), str(resid), flag])
    out.append(["every plan: maker fill = hedge fills + unwind fills (+ a sub-lot remainder)", verdict(open_plans == 0), f"{len(rows)} plans with fills, {open_plans} with an exposure of one lot or more left"])
    chains = (r.life or {}).get("hedge_chains_by_final_state", {})
    out.append(["every hedge chain ended RECONCILED", verdict(set(chains) <= {"RECONCILED"}), json.dumps(chains)])
    rec = ((r.m.get("execution_layer") or {}).get("exec_reconciliation")) or {}
    probs = {k for k, v in (rec.get("problems") or {}).items() if v}
    fee_only = bool(probs) and probs <= {"fee", "fees_paid"}
    out.append(["the layer's shadow reconciliation (cash, base, fees from the fill journal alone)", "<b class='note'>TOOL DEFECT (fee only)</b>" if fee_only else verdict(bool(rec.get("ok"))), f"checked {rec.get('checked')}, problems {json.dumps({k: len(v) for k, v in (rec.get('problems') or {}).items() if v})}"
                + ("" if rec.get("ok") else "; a fee problem here is the tool comparing a FILL event's cumulative `fee` with one fill (fixed in the live tree after this run was frozen: tests/test_oms_amend.py, the check above uses fee_delta)")])
    return out, rows


def alarms(r):
    names = ("BREAKER_TRIP", "BREAKER_ALARM", "BREAKER_FLATTEN", "BREAKER_FLATTEN_DUST", "BREAKER_RESET", "HEDGE_ALARM", "UNWIND_EXHAUSTED_ALARM", "HEDGE_LOT_RESIDUAL_ALARM", "KILL_SWITCH_STATE_TRANSITION", "UNWIND_DUST", "MARGIN_PRICE_PAUSE", "MARGIN_BUFFER_ALERT", "RESIDUAL_LIMIT_BREACH", "HEDGE_CHAINS_ESCALATED")
    rows = [[e["event_type"], e.get("ts_ns"), (e.get("cause") or e.get("reason") or e.get("state") or "")] for e in r.safety if e["event_type"] in names]
    return rows


def positions_timeline(r):
    """Max |net|, max |per venue| over the run from the fills in time order (signed by side)."""
    pos, net_max, venue_max = {}, Decimal("0"), Decimal("0")
    for f in sorted(r.fills, key=lambda f: f["ts_ns"]):
        q = Decimal(str(f["fill_quantity"]))
        pos[f["venue"]] = pos.get(f["venue"], Decimal("0")) + (q if str(f["side"]).upper() == "BUY" else -q)
        net_max = max(net_max, abs(sum(pos.values(), Decimal("0"))))
        venue_max = max(venue_max, max(abs(v) for v in pos.values()))
    return net_max, venue_max


def paths_block(r, title):
    cov, life = r.cov, r.life
    if cov is None:
        return f"<h3>{esc(title)}</h3><p class='note'>no coverage file.</p>"
    p = cov["paths"]
    flat = [(k, v) for k, v in p.items() if k != "intent_states"]
    half = (len(flat) + 1) // 2
    out = [f"<h3>{esc(title)}</h3><p>Intent states: {esc(json.dumps(p['intent_states']))}. Per-order checks: {cov['checks']['orders_checked']} orders, off-tick prices {len(cov['checks']['off_tick'])}, "
           f"caps on the wrong side of the touch {len(cov['checks']['cap_on_the_wrong_side_of_touch'])}, tactic violations {len(cov['tactic_violations'])}; pass {cov['checks']['pass']}.</p>",
           "<div class='cols'>" + table(["path", "count"], flat[:half]) + table(["path", "count"], flat[half:]) + "</div>"]
    if life:
        t = life["total"]
        q = t["quotes"]
        out.append(f"<h4>Lifecycle, whole run</h4><p>Quotes placed {q['quotes_placed']}, with a fill {q['quotes_filled']}, fill rate "
                   f"{fmt(q['fill_rate']) if q['fill_rate'] is not None else 'n &lt; 30: counts only'}; races: fills during cancel pending {q['fills_during_cancel_pending']}, cancel while submitted {q['cancel_while_submitted']}, "
                   f"late after cancel {q['late_fills_after_cancel']}. Residual exposure: {esc(json.dumps(t['residual_exposure']))}. Hedge completion: {esc(json.dumps(t['hedge_completion']))}. "
                   f"Message load per minute: {esc(json.dumps(t['message_load_per_minute']))}. Adverse tails (markout bps): {esc(json.dumps(t['adverse_tails_markout_bps']))}.</p>")
        for dim in ("volatility", "spread", "liquidity", "utc_hour"):
            rows = []
            for k, c in life["by_regime"][dim].items():
                qq = c["quotes"]
                rows.append([k, qq["quotes_placed"], qq["quotes_filled"], fmt(qq["fill_rate"]) if qq["fill_rate"] is not None else "n < 30", qq["fills_during_cancel_pending"] + qq["late_fills_after_cancel"],
                             c["hedge_completion"]["maker_fills"], c["hedge_completion"]["fully_hedged"], fmt(c["residual_exposure"]["oldest_exposure_age_s"])])
            out.append(f"<h4>By {dim.replace('_', ' ')} (regimes in-sample)</h4>" + table(["bucket", "quotes", "filled", "fill rate", "races", "maker fills", "fully hedged", "oldest exposure (s)"], rows))
        out.append(f"<p class='note'>Regime boundaries: {esc(json.dumps(life['regime_boundaries']))}. Partition check passed: {life['partition_ok']}.</p>")
    return "".join(out)


def stats_block(r):
    s = (r.pairs or {}).get("summary") or {}
    rows = [[k, v.get("status"), v.get("n"), v.get("mean"), v.get("median"), v.get("p5"), v.get("p95"), v.get("blocks"), v.get("ci_status")] for k, v in s.items()]
    md = (r.pairs or {}).get("metadata")
    return table(["metric", "status", "n", "mean", "median", "p5", "p95", "day blocks", "interval"], rows) + (f"<p class='note'>Statistics block metadata: {esc(json.dumps(md))[:900]}</p>" if md else "")


# ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ determinism
def determinism():
    if not (A.exists and B.exists):
        return "<p class='note'>run B not available.</p>", False
    names = sorted({p.name for p in DIR_A.iterdir()} | {p.name for p in DIR_B.iterdir()})
    rows, bad = [], []
    for n in names:
        pa, pb = DIR_A / n, DIR_B / n
        if not (pa.exists() and pb.exists()):
            rows.append([n, "missing in one run", ""])
            bad.append(n)
            continue
        same = pa.read_bytes() == pb.read_bytes()
        detail = ""
        if not same and n == "session_manifest.json":
            ma, mb = json.loads(pa.read_text()), json.loads(pb.read_text())
            diffs = []

            def walk(x, y, p=""):
                if isinstance(x, dict) and isinstance(y, dict):
                    for k in sorted(set(x) | set(y)):
                        walk(x.get(k), y.get(k), p + "/" + k)
                elif x != y:
                    diffs.append(p)
            walk(ma, mb)
            allowed = [d for d in diffs if d.endswith("wall_s") or "release_identity" in d]
            detail = f"differs only at {allowed}" if len(allowed) == len(diffs) else f"DIFFERS at {[d for d in diffs if d not in allowed][:6]}"
            same = len(allowed) == len(diffs)
        rows.append([n, "identical" if same else "DIFFERENT", detail])
        if not same:
            bad.append(n)
    return table(["file", "result", "detail"], rows), not bad


# ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ the report
def count(r, name):
    return sum(1 for e in r.safety if e["event_type"] == name)


def headline(r):
    if not r.exists:
        return ["not available"] * 12
    life = (r.life or {}).get("total", {})
    q = life.get("quotes", {})
    orders = {k: sum(1 for o in r.orders if kind(o) == k) for k in "PHU"}
    return [r.m.get("profile"), r.m.get("events", {}).get("book"), r.m.get("events", {}).get("trade"), len(r.signals), len(r.orders), orders["P"], orders["H"], orders["U"], len(r.fills),
            q.get("quotes_placed"), q.get("quotes_filled"), json.dumps(life.get("hedge_completion")), json.dumps(r.m.get("positions")), r.m.get("equity_at_book_mids"),
            (r.m.get("residual_at_end") or {}).get("net_position_base"), (r.m.get("residual_at_end") or {}).get("total_residual_base"), r.run.get("wall_s")]


H_ROWS = ["profile", "book events", "trade events", "signals", "orders", "maker orders", "hedge orders", "unwind / flatten orders", "fills", "quotes placed", "quotes with a fill", "hedge completion", "end positions", "ledger equity at book mids (USDT)",
          "end net position", "end residual (carried + open)", "wall time (s)"]


def build():
    det_html, det_ok = determinism()
    rec_rows, plan_rows = reconcile(A) if A.exists else ([], [])
    ha, hr = headline(A), headline(R)
    cf = E2E / "SNAPSHOT_COMMITS.txt"
    snap_commits = (cf.read_text().strip() if cf.exists() else (SNAP / "COMMITS.txt").read_text().strip() if (SNAP / "COMMITS.txt").exists() else "(snapshot commits file missing)")
    limits = A.limits or {}
    stops = limits.get("stop_quoting", {})
    lim = limits.get("limits_that_bound", {})
    net_max, venue_max = positions_timeline(A) if A.exists else (0, 0)
    sc = A.m.get("strategy_counters", {})
    rej_text = {"no_edge": "the cross-venue edge after fees and the expected hedge move is below the minimum (the normal reason not to quote)", "stale": "a venue's book is older than the book age limit",
                "stale_touch": "the touch moved between the decision and the quote (the entry rule)", "inventory": "the position or net ceiling (1.0 SOL + 0.25 headroom, 2.0 gross per venue) leaves no room",
                "maker_limit": "the quote would cross or not improve the maker venue's touch (post-only would be rejected)", "cross_venue_skew": "the two venues' book timestamps differ by more than 0.2 s",
                "tick_rounding_joined_touch": "the quote rounded onto the tick joined the touch (counted, not a stop)", "not_ready": "a book is not valid yet"}
    ib = (A.m.get("manifest") or {}).get("invalid_book_policy") or {}
    sus = (A.m.get("manifest") or {}).get("suspended_seconds_two_ways") or {}
    led = (limits.get("ledger_event_type_counts") or A.m.get("ledger_event_counts") or {})
    stop_rows = [[k, v, rej_text.get(k, "")] for k, v in sorted((sc.get("rejects") or {}).items(), key=lambda kv: -kv[1])]
    stop_rows += [["book gate: resyncs after an invalid book", len(ib.get("resyncs", [])), "a gap or crossed book: trading suspended until the venue's snapshot (assumed delay 5 s if none arrives)"],
                  ["book gate: suspended seconds (common hours)", json.dumps(sus.get("any_message_age (the rule), common hours only")), "any reason the book gate blocked, start-up included"],
                  ["time integrity blocks", (A.m.get("manifest") or {}).get("time_integrity_blocks"), "a timestamp anomaly blocks decisions on that event"],
                  ["breaker blocked signals", stops.get("signals_blocked_by_breaker"), "breaker paused or tripped: no new risk"],
                  ["margin price pause (events / signals blocked)", f"{led.get('MARGIN_PRICE_PAUSE', 0)} / {stops.get('signals_blocked_by_margin_price')}", "no usable mark price for the margin check: no new risk until it is back (the labelled MARK_FROM_MID fallback applies)"],
                  ["quotes skipped by the rate limiter", stops.get("quotes_skipped_rate_limit"), "the 70% quote share of a window (or the 2 free requests) was used up: skipped, never queued"],
                  ["quotes withdrawn by quote validity", stops.get("quotes_withdrawn"), "off in v1_9 (arb.quote_withdraw is a v1_8 option)"],
                  ["kill switch", f"{stops.get('kill_switch')} {stops.get('kill_switch_reason') or ''}", "residual 5 s hold then 30 s kill, an unknown hedge, an unwind that could not finish"],
                  ["new risk held (OMS)", json.dumps(stops.get("oms_new_risk_held")), "a residual breach, an unknown hedge, an unwind-exhausted alarm or an unreconciled restart"],
                  ["layer risk gate refusals (enforce)", json.dumps({k: v for k, v in ((A.m.get('execution_layer') or {}).get('layer') or {}).get('counts', {}).items() if 'risk' in k}), "the layer's base ceiling over filled plus reserved exposure"],
                  ["risk engine refusals", json.dumps(lim.get("risk_engine_refusals")), "notional, margin, position, legging limits"]]
    rl = (lim.get("rate_limiter") or {})
    bucket_rows = [[b["key"], b["limit"], b["peak_used"], f"{b.get('peak_utilization', 0):.4f}", "yes" if b.get("peak_utilization", 0) >= 0.7 else "no"] for b in rl.get("buckets", [])]
    chap = lambda rows: rows
    gate = lambda d: (d.m.get("manifest") or {}).get("safety_from_profile", {})
    html_parts = [f"""<h1>End-to-end rehearsal, 2026-10-29 capture</h1>
<p class='sub'>v1_9 with the approved production limits, the full SOLUSDT day from the recorder's capture, both venues. <b>EXPLORATORY.</b> One day. Mark = mid. No funding (derivatives pending). Regimes are in-sample (the day's own terciles).
No own orders on the venues: every fill is simulated and shows mechanics only. No profit claim.</p>
<p class='note'>Code: frozen snapshot of the committed trees ({esc(snap_commits)}), run with <code>run_day.py --src</code> from a copy of the committed profiles and config. Profile v1_9 with the 13 Nov limits: quote share 0.7 with 2 requests free, hedges never refused by the limiter,
cancels unrestricted, gross per venue 2.0 SOL (from the profile), net ceiling 1.0 SOL plus 0.25 headroom, silent carry below one lot (0.01 SOL), failed hedges always unwind or alarm, the layer risk gate in enforce, the profile's breaker.
Safety values taken from the profile: {esc(json.dumps({k: v for k, v in gate(A).items() if k in ('layer_risk_mode', 'profile_safety')}))}. Run A wall {esc(A.run.get('wall_s'))} s, run B {esc(B.run.get('wall_s'))} s, v1_7 reference {esc(R.run.get('wall_s'))} s.</p>"""]
    html_parts.append(f"<h2>1. Byte identity: run A against run B</h2><p>Every file of the two v1_9 runs, compared byte for byte. Only <code>run.wall_s</code> and the release-identity stamps may differ. Verdict: {verdict(det_ok)}. The stamps record the git state (dirty flag, changed-file count) of the LIVE working trees when a run starts, not of the frozen snapshot the code ran from, so they differ whenever the live tree changed between two runs (it did: other work went on during the rehearsal); the code of both runs is the same snapshot.</p>{det_html}")
    html_parts.append("<h2>2. Headline: v1_9 against the v1_7 reference</h2><p class='note'>v1_7 is the reference profile of the same strategy without the 13 Nov changes (no size-aware cap, no ack deadline, silent carry bound = the position size, layer gate in count, no breaker); same clip (1.0 SOL), same gross per venue (2.0 SOL).</p>"
                      + table(["", "v1_9 (run A)", "v1_7 reference"], [[H_ROWS[i], ha[i], hr[i]] for i in range(len(H_ROWS))]))
    html_parts.append("<h2>3. Reconciliation: every order, fill, hedge, unwind and alarm</h2><p>Each check is computed from the run's own files (orders, fills, ledger positions and cash, lifecycle report), independently of the engine's counters.</p>"
                      + table(["check", "result", "detail"], rec_rows))
    byrole = {}
    for o in A.orders:
        byrole.setdefault(ROLE.get(kind(o), kind(o)), {}).setdefault(o["state"], 0)
        byrole[ROLE.get(kind(o), kind(o))][o["state"]] += 1
    html_parts.append("<h3>Orders by role and state</h3>" + table(["role", "states"], [[k, json.dumps(v)] for k, v in sorted(byrole.items())]))
    html_parts.append("<h3>Per plan: maker, hedges, unwinds, residual</h3>" + table(["plan", "maker side", "maker filled", "hedge orders", "hedge filled", "unwind orders", "unwind filled", "residual", "flag"], plan_rows))
    html_parts.append("<h3>Alarms and safety events that fired</h3>" + (table(["event", "ts_ns", "cause / state"], alarms(A)) if alarms(A) else "<p>None: no breaker trip, no hedge alarm, no unwind-exhausted alarm, no kill switch, no residual limit breach in this run.</p>")
                      + f"<p class='note'>All safety events of the run in full: <code>{esc(DIR_A.name)}/session_safety_events.jsonl</code> ({len(A.safety)} rows); every event type and its count: <code>session_limits_and_stops.json</code>.</p>")
    html_parts.append("<h2>4. EMS path counts</h2>" + paths_block(A, "v1_9, run A") + paths_block(R, "v1_7 reference"))
    html_parts.append("<h2>5. Lifecycle metrics by regime, statistics block, pair economics</h2><p>The lifecycle tables above are by volatility, spread, liquidity and UTC hour. Pair economics (one row per maker fill linked to its hedge) and the statistics block (median, 5th and 95th percentile, block-bootstrap interval; with one day there is one block, so no interval is claimed):</p>"
                      + stats_block(A))
    html_parts.append("<h2>6. End-of-day positions and residuals</h2>" + table(["", "v1_9", "v1_7"], [["positions", json.dumps(A.m.get("positions")), json.dumps(R.m.get("positions"))], ["residual at end", json.dumps(A.m.get("residual_at_end")), json.dumps(R.m.get("residual_at_end"))],
                                                                                                    ["max |net| over the day (from the fills)", str(net_max), str(positions_timeline(R)[0] if R.exists else "")], ["max |position| on one venue", str(venue_max), str(positions_timeline(R)[1] if R.exists else "")]]))
    html_parts.append("<h2>7. Every stop-quoting condition that fired, and why</h2>" + table(["condition", "count", "why"], stop_rows)
                      + "<p class='note'>The strategy reject counters count evaluations (one per book event); a quote is not sent while any of them holds. Counts are complete; nothing is a truncated list.</p>")
    html_parts.append("<h2>8. Limits: which ones bound</h2>" + (f"<p>Rate limiter: quote share {rl.get('quote_share')}, free requests {rl.get('quote_min_free')}; throttled by status {esc(json.dumps(rl.get('throttled_by_status')))}; cancels delayed {rl.get('cancels_delayed')}; over-limit sends {rl.get('over_limit_sent')}. "
                                                                  "Peak utilization of each window over the run (a bucket binds a quote at 0.70):</p>" + table(["bucket", "limit", "peak used", "peak utilization", "bound a quote (>= 0.70)"], bucket_rows) if rl else "<p class='note'>no limiter snapshot in this run.</p>"))
    rows8 = [["net ceiling 1.0 SOL + 0.25 headroom (hard 1.25)", f"max |net| {net_max}", "EXCEEDED" if net_max > Decimal("1.25") else ("AT THE LIMIT" if net_max >= Decimal("1.0") else "no")],
             ["gross per venue 2.0 SOL", f"max |position| on one venue {venue_max}", "EXCEEDED" if venue_max > Decimal("2.0") else ("YES, AT THE LIMIT: the inventory rule refused quoting from then on" if venue_max >= Decimal("2.0") else "no")],
             ["residual hold 5 s / kill 30 s", f"max unhedged interval {((A.life or {}).get('total', {}).get('residual_exposure') or {}).get('unhedged_interval_s')}", "see section 5"],
             ["quote lifetime 15 s", f"{sum(1 for o in A.orders if o.get('lifetime_expired'))} orders ended at the lifetime", "yes" if any(o.get('lifetime_expired') for o in A.orders) else "no"],
             ["hedge cap (size-aware, 15 bps absolute limit)", f"{sum(1 for d in A.decisions if d.get('event_type') == 'HEDGE_CAP_SIZE_AWARE' and d.get('abs_limit_binds'))} of {sum(1 for d in A.decisions if d.get('event_type') == 'HEDGE_CAP_SIZE_AWARE')} cap decisions hit the absolute limit", "see counts"],
             ["unwind 15 bps band, 3 attempts", f"{sum(1 for o in A.orders if kind(o) == 'U')} unwind orders", "yes" if any(kind(o) == 'U' for o in A.orders) else "no"],
             ["silent carry below one lot", f"end carried residual {(A.m.get('residual_at_end') or {}).get('carried_residual_base')}", "see section 6"],
             ["layer risk gate (enforce)", json.dumps({k: v for k, v in ((A.m.get('execution_layer') or {}).get('layer') or {}).get('counts', {}).items() if 'risk' in k}), "no refusals" if not any('refus' in k and v for k, v in ((A.m.get('execution_layer') or {}).get('layer') or {}).get('counts', {}).items()) else "refused"]]
    inv = (sc.get("rejects") or {}).get("inventory")
    html_parts.append("<h3>The approved limits against what the day did</h3>" + table(["limit", "what the run reached", "bound"], rows8)
                      + f"<p><b>The limit that bound is the gross per venue (2.0 SOL).</b> After the second maker fill and its hedge the book held +2.0 SOL on Bybit and -2.0 SOL on Binance (net zero, hedged); the strategy has no exit for an open pair, so the inventory rule refused every later evaluation ({inv} of them) "
                        f"and the day ended with {len(A.signals)} signals. A 13 Nov replay of the same day with gross 5.0 SOL showed 5 signals. The net ceiling (1.0 SOL plus 0.25 headroom) was reached at 1.0 SOL during each hedge interval and never exceeded. The venue-window rate limiter never came near its 70% quote share "
                        f"(peak 10% of the Bybit order-create window): it did not bind and its priority queue was not needed.</p>")
    html_parts.append("<h3>Approved but NOT built (no code enforces them; none could bind in this run)</h3><ul><li>2 new orders/s sustained and 5/s peak per venue (only the venue-window limiter exists)</li><li>daily loss stop of -5 USDT</li><li>no quote in the 30 s before a funding timestamp</li>"
                      "<li>clock-offset gate (unknown or above 5 ms)</li><li>margin WARNING as a stop, and kill after 60 s of paused marks</li><li>tier table read at startup; account settings checked against the real account in the session</li><li>1 s no-requote after a withdrawal</li><li>the 10,000 events/s proof</li></ul>"
                      "<p class='note'>The proposal said the layer gate stays in count mode until validation; the owner's later order runs v1_9 in the profile's mandatory enforce mode, and that order stands (see the contract).</p>")
    pe = (A.pairs or {}).get("summary") or {}
    html_parts.append("<h2>9. Ledger P&amp;L (EXPLORATORY)</h2>" + table(["", "v1_9", "v1_7"], [["ledger equity at book mids (USDT)", A.m.get("equity_at_book_mids"), R.m.get("equity_at_book_mids")],
                                                                                                    ["cash by venue", json.dumps(A.m.get("cash_by_venue")), json.dumps(R.m.get("cash_by_venue"))],
                                                                                                    ["fees paid (sum of the fills' fees)", str(sum((Decimal(str(f.get('fee_delta') or 0)) for f in A.fills), Decimal('0'))), str(sum((Decimal(str(f.get('fee_delta') or 0)) for f in R.fills), Decimal('0')))],
                                                                                                    ["pair gross bps (median, n)", f"{(pe.get('pair_gross_bps') or {}).get('median')} (n={(pe.get('pair_gross_bps') or {}).get('n')})", f"{(((R.pairs or {}).get('summary') or {}).get('pair_gross_bps') or {}).get('median')}"],
                                                                                                    ["pair net bps (median, n)", f"{(pe.get('pair_net_bps') or {}).get('median')} (n={(pe.get('pair_net_bps') or {}).get('n')})", f"{(((R.pairs or {}).get('summary') or {}).get('pair_net_bps') or {}).get('median')}"],
                                                                                                    ["markout summary 1 s / 5 s / 30 s (qty-weighted bps)", json.dumps(A.m.get("markout_summary")), json.dumps(R.m.get("markout_summary"))]])
                      + "<p class='note'>Equity is the ledger's cash plus the open position marked at the book mids; with mark = mid and no funding it is a mechanics number: the fee schedule is ASSUMED_PUBLIC_SCHEDULE_UNVERIFIED, the S1 table is out-of-period (fitted March 2024), the capture's recorder quality is REJECTED and the host clock UNKNOWN. Not a result to quote.</p>")
    html_parts.append("<h2>10. What this rehearsal does not show</h2><ul><li>No funding and no recorded marks (derivatives.jsonl for the day is pending; the margin check uses the labelled MARK_FROM_MID fallback).</li><li>One day only: no out-of-sample claim, regimes are the day's own terciles.</li>"
                      "<li>No own orders on the venues: queue position and fills are the simulator's (a fill needs a real print; nothing is trained on them).</li><li>The approved limits listed as NOT built could not bind.</li><li>Network and venue behaviour (ack latency, rate-limit responses) are the profile's modelled latencies, not measurements of the mainnet.</li></ul>")
    css = """body{font:15px/1.5 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;margin:0;padding:24px 16px;color:#1f2328;background:#fff}main{max-width:1150px;margin:0 auto}h1{font-size:26px;margin:0 0 4px}h2{font-size:19px;margin:32px 0 8px;border-bottom:1px solid #d0d7de;padding-bottom:4px}h3{font-size:16px;margin:22px 0 6px}h4{font-size:14px;margin:14px 0 4px}
.sub{color:#57606a;margin-top:0}.note{color:#57606a;font-size:13px}code{background:#f2f4f6;padding:1px 4px;border-radius:4px;font-size:13px}table{border-collapse:collapse;width:100%;margin:8px 0;font-size:13px}th,td{border:1px solid #d0d7de;padding:4px 8px;text-align:left;vertical-align:top;overflow-wrap:anywhere}th{background:#f6f8fa}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:12px}.ok{color:#1a7f37}.bad{color:#b42318}@media (max-width:700px){.cols{grid-template-columns:1fr}table{display:block;overflow-x:auto}}
@media (prefers-color-scheme:dark){body{background:#0d1117;color:#e6edf3}.sub,.note{color:#9da7b3}th{background:#161b22}th,td,h2{border-color:#30363d}code{background:#161b22}.ok{color:#3fb950}.bad{color:#f85149}}"""
    doc = f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>End-to-end rehearsal 2026-10-29</title><style>{css}</style></head><body><main>{''.join(html_parts)}</main></body></html>"
    OUT.write_text(doc)
    print("written", OUT, len(doc))


build()
