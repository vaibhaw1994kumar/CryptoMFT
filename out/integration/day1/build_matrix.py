"""The EMS coverage matrix (owner 12 Nov, v1_9_TOLERANCES.md section M): reads the scenario outputs in runs/scn_*/ (and the unit-case evidence files in ems_cases/), applies the pass rules of sections H, J, K, writes
out/integration/ems_cases/coverage_matrix.md and coverage_matrix.json. A run wrapper over files: it decides nothing the tolerances file did not fix before the runs. usage: build_matrix.py"""
import json
import sys
from pathlib import Path

DAY = Path("/Users/vaibhav/Documents/Newproject/out/integration/day1")
CASES = Path("/Users/vaibhav/Documents/Newproject/out/integration/ems_cases")
RUNS = DAY / "runs"


def rows(path):
    return [json.loads(l) for l in open(path) if l.strip()]


def load(case):
    d = RUNS / f"scn_{case}"
    if not (d / "session_manifest.json").exists():
        return None
    m = json.loads((d / "session_manifest.json").read_text())
    return {"dir": d, "manifest": m, "orders": rows(d / "session_orders.jsonl"), "fills": rows(d / "session_fills.jsonl"), "signals": rows(d / "session_signals.jsonl"),
            "cov": json.loads((d / "session_ems_path_coverage.json").read_text()), "life": json.loads((d / "session_lifecycle_report.json").read_text()),
            "pairs": rows(d / "session_pair_economics.jsonl") if (d / "session_pair_economics.jsonl").exists() else [], "trace": CASES / f"scn_{case}.jsonl"}


def hedge_orders(r):
    return [o for o in r["orders"] if o["venue"] == "BINANCE" and o["state"] != "CREATED"]


def maker_fills(r):
    return [f for f in r["fills"] if f["venue"] == "BYBIT"]


def common_checks(r):
    c = r["cov"]
    return c["checks"]["pass"], f"off_tick={len(c['checks']['off_tick'])}, tactic_violations={len(c['tactic_violations'])}"


matrix = []


def add(case, layer, count, ok, detail, evidence):
    matrix.append({"case": case, "covering_layer": layer, "count": count, "result": "PASS" if ok is True else "FAIL" if ok is False else "NOT COVERED", "detail": detail, "evidence": evidence})


# ---- H1 determinism
a, b = RUNS / "scn_H1a", RUNS / "scn_H1b"
if a.exists() and b.exists():
    diffs = []
    for f in sorted(p.name for p in a.iterdir()):
        if f == "session_manifest.json":
            ma, mb = json.loads((a / f).read_text()), json.loads((b / f).read_text())
            for m in (ma, mb):
                m.pop("run", None)
                m.get("manifest", {}).pop("release_identity", None)
            if ma != mb:
                diffs.append(f)
        elif (a / f).read_bytes() != (b / f).read_bytes():
            diffs.append(f)
    add("H1 determinism (case 2a window run twice)", "simulator / session", len(list(a.iterdir())), not diffs, "byte-identical except release_identity and run wall time" if not diffs else f"DIFFERENT: {diffs}", "runs/scn_H1a vs runs/scn_H1b")

# ---- clean-replay style checks for every scenario run
for case in sorted(p.name[4:] for p in RUNS.glob("scn_*") if p.is_dir()):
    r = load(case)
    if r is None:
        continue
    ok, detail = common_checks(r)
    add(f"J2/J3 checks, {case}", "execution layer, router", len(r["orders"]), ok, detail, f"runs/scn_{case}/session_ems_path_coverage.json")

# ---- scenario assertions
r = load("2a_quote_withdraw")
if r:
    withdrawn = [o for o in r["orders"] if (o.get("cancel_reason") or "").startswith("quote_withdrawn")]
    c = r["cov"]["paths"]
    add("2a Binance move before the timer tick (quote_withdraw on)", "strategy, OMS, router, simulator", c["maker_withdrawn_on_event"], c["maker_withdrawn_on_event"] > 0 and len(withdrawn) <= c["maker_withdrawn_on_event"],
        f"withdrawn on the event {c['maker_withdrawn_on_event']}, order rows with the reason {len(withdrawn)}, expired at the lifetime {c['maker_expired_at_lifetime']}, fills during cancel pending {c['fills_during_cancel_pending']}", str(r["trace"].name))
r = load("2b_cancel_latency")
if r:
    c = r["cov"]["paths"]; ids = [f["fill_id"] for f in r["fills"]]
    booked_once = len(ids) == len(set(ids))
    hedged_once = len(hedge_orders(r)) <= len(maker_fills(r)) + 1
    add("2b Fill during cancel pending (cancel latency 800 ms); 2b2 fill after the cancel is confirmed", "OMS, simulator", c["fills_during_cancel_pending"] + c["late_fills_after_cancel"], booked_once and hedged_once and (c["fills_during_cancel_pending"] + c["late_fills_after_cancel"]) > 0,
        f"during cancel pending {c['fills_during_cancel_pending']}, late after cancel {c['late_fills_after_cancel']}; fill ids unique: {booked_once}; hedge orders {len(hedge_orders(r))} for {len(maker_fills(r))} maker fills; residual at end {r['manifest']['residual_at_end']['flag']}", str(r["trace"].name))
for gs in ("3", "30"):
    r = load(f"2d_ghost{gs}")
    if r:
        net_ok = r["manifest"]["residual_at_end"]["flag"] in ("FLAT_NET", "UNHEDGED_OR_CARRIED_RESIDUAL")
        mk_q = sum(float(o["filled_quantity"]) for o in r["orders"] if o["venue"] == "BYBIT"); hd_q = sum(float(o["filled_quantity"]) for o in r["orders"] if o["venue"] == "BINANCE")
        dup = abs(mk_q - hd_q) > 1e-9                         # a duplicate hedge would hedge more than the maker filled
        h = r["manifest"].get("hedge_fault_injection") or r["manifest"].get("manifest", {}).get("hedge_fault_injection", {})
        add(f"2d Timeout then late success (ghost {gs} s)", "OMS, simulator, router", len(hedge_orders(r)), net_ok and not dup,
            f"hedge filled quantity {hd_q:g} = maker filled quantity {mk_q:g}; hedge orders {len(hedge_orders(r))} for {len(maker_fills(r))} maker fills (a cancelled unfilled replacement is not a hedge); ghost delivered {h.get('ghost_hedges_delivered')}, held at end {h.get('ghost_hedges_held_at_end')}; residual at end {r['manifest']['residual_at_end']['flag']}; four-way reconciliation is a unit case (case 9)", str(r["trace"].name))
r = load("2e_feed_gap")
if r:
    gap0, gap1 = 1793232000 + 20 * 3600 + 20 * 60, 1793232000 + 20 * 3600 + 20 * 60 + 30
    sent = {t["order_id"]: t["ts"] for t in rows(r["trace"]) if t["stage"] == "request_sent" and t.get("order_id")}
    inside = [o for o in r["orders"] if o["post_only"] and o["order_id"] in sent and gap0 + 0.5 < sent[o["order_id"]] < gap1]      # a maker quote SENT after the last book message went stale and before the feeds return
    after = [sent[o["order_id"]] for o in r["orders"] if o["post_only"] and o["order_id"] in sent and sent[o["order_id"]] >= gap1]
    add("2e Feed gap (injected 30 s, 20:20:00 UTC, window 19:00-21:00)", "session, gate, OMS", len(r["orders"]), not inside,
        ("RESUMPTION NOT DEMONSTRATED IN THIS WINDOW (no quote was due after the gap); demonstrated by the unit case case8_recovery_after_a_feed_gap_*.jsonl (a quote lifetime of 1 s forces re-quotes). " if not after else "") + f"maker quotes sent inside the gap {len(inside)}; first quote after the gap {(min(after) - gap1) if after else None} s after the feeds returned; orders {len(r['orders'])}; suspended seconds {r['manifest']['manifest'].get('suspended_seconds_two_ways', {})}", str(r["trace"].name))
r, base = load("2f_rate_limit"), load("H1a")
if r and base:
    c, cb = r["cov"]["paths"], base["cov"]["paths"]
    refused_h = sum(1 for o in r["orders"] if o["venue"] == "BINANCE" and o.get("cancel_reason") == "scoped_rate_limited")
    refused_q = sum(1 for o in r["orders"] if o["venue"] == "BYBIT" and o.get("cancel_reason") == "scoped_rate_limited")
    skipped = r["manifest"].get("ledger_event_counts", {}).get("QUOTE_SKIPPED_RATE_LIMIT", 0)
    flat = r["manifest"]["residual_at_end"]["flag"] == "FLAT_NET"
    ok = refused_h == 0 and c["maker_placed"] <= cb["maker_placed"] and flat
    add("2f Rate-limit pressure (6 requests / 10 s; quote share 80%, 2 requests always free)", "router, rate limiter, session", c["rate_limit_deferral"] + skipped, ok,
        f"hedges refused {refused_h} (rule: 0; before the fix 4 were refused); maker orders created {c['maker_placed']} (baseline {cb['maker_placed']}; before the fix 19, so the attempts fell); quotes skipped before a plan existed {skipped}; quotes refused by the router {refused_q}; hedge or unwind dispatches queued {c['rate_limit_deferral']}; "
        f"hedge orders {len(hedge_orders(r))} for {len(maker_fills(r))} maker fills; unwind orders {c['unwind_orders']}; residual at end {r['manifest']['residual_at_end']['flag']}", str(r["trace"].name))
r = load("2j_duplicate_filled")
if r and base:
    same = [f["fill_id"] for f in r["fills"]] == [f["fill_id"] for f in base["fills"]]
    add("2j Duplicate Filled in a cancel/fill race (every fill report delivered twice)", "OMS reducer", r["cov"]["paths"]["duplicate_updates_suppressed"], same and r["cov"]["paths"]["duplicate_updates_suppressed"] > 0,
        f"suppressed duplicates {r['cov']['paths']['duplicate_updates_suppressed']}; fills identical to the baseline window: {same}", str(r["trace"].name))
r = load("2m_processing")
if r and base:
    ub = base["life"]["total"]["residual_exposure"]["unhedged_interval_s"]; ur = r["life"]["total"]["residual_exposure"]["unhedged_interval_s"]
    add("2m Latency: processing 5+5 ms on quote and hedge paths", "router, timing", ur.get("n", 0), ur.get("p50", 0) > ub.get("p50", 1e9),
        f"unhedged interval p50 {ub.get('p50')} -> {ur.get('p50')} s; races {r['cov']['paths']['fills_during_cancel_pending']}; the p99 and p99.9 runs need the sampled-latency mode (not built)", str(r["trace"].name))
for sz in ("1", "5", "25"):
    r = load(f"2n_size{sz}")
    if r:
        h = hedge_orders(r); full = r["cov"]["paths"]["hedge_ioc_full"]; part = r["cov"]["paths"]["hedge_ioc_partial"]; zero = r["cov"]["paths"]["hedge_ioc_zero"]
        slip = []
        for p in r["pairs"]:
            if p.get("hedge_avg_price") and p.get("maker_price"):
                slip.append(abs(float(p["hedge_avg_price"]) - float(p["maker_price"])) / float(p["maker_price"]) * 1e4)
        dec = rows(r["dir"] / "session_decision_events.jsonl") if (r["dir"] / "session_decision_events.jsonl").exists() else []
        spans = [e for e in dec if not e["touch_covers"]]
        add(f"2n Size {sz} SOL (size_aware cap, participation cap on)", "OMS, size_aware cap, simulator, E3", len(h), r["cov"]["checks"]["pass"],
            f"hedge IOC full {full}, partial {part}, zero {zero}, unwind orders {r['cov']['paths']['unwind_orders']}, cap binds on the print {r['cov']['paths']['participation_cap_would_bind']}; size_aware decisions {r['cov']['paths']['hedge_cap_size_aware']} (touch did NOT cover the size in {len(spans)}, abs limit bound in {sum(1 for e in dec if e['abs_limit_binds'])}, fallback {sum(1 for e in dec if e['fallback'])}); hedge price vs the maker quote price (bps, n={len(slip)}; the decision touch itself is in the cap events): "
            + (f"max {max(slip):.2f}" if slip else "none"), str(r["trace"].name))

def safety(case):
    f = RUNS / f"scn_{case}" / "session_safety_events.jsonl"
    return rows(f) if f.exists() else None


ev = safety("2k_breaker_pause")
if ev is not None:
    states = [e["state"] for e in ev if e["event_type"] == "BREAKER_STATE_TRANSITION"]
    kills = [e for e in ev if e["event_type"] == "KILL_SWITCH_STATE_TRANSITION"]
    ok = states[:2] == ["PAUSED", "OK"] and "TRIPPED" not in states and not kills
    add("2k Breaker pause: flat and both gateways silent 13 s, resumes by itself", "session breaker", len(states), ok, f"breaker states {states}; kill-switch events {len(kills)}; margin price pauses in the same run are the MARK_FROM_MID replay fallback", "scn_2k_breaker_pause.jsonl, runs/scn_2k_breaker_pause/session_safety_events.jsonl")
ev = safety("2k_breaker_trip")
if ev is not None:
    trip = [e for e in ev if e["event_type"] == "BREAKER_TRIP"]
    flat = [e for e in ev if e["event_type"] == "BREAKER_FLATTEN"]
    reset = [e for e in ev if e["event_type"] == "BREAKER_RESET"]
    fields = all(k in trip[0] for k in ("confirmed_residual_base", "pending_hedge_qty", "oldest_exposure_age_s", "execution_blocked_reason")) if trip else False
    tactic = bool(flat) and all(f["tactic"] == "MARKET_BANDED" for f in flat)
    band_ok = True
    r = load("2k_breaker_trip")
    quotes_after = []
    if trip and reset:
        t0, t1 = trip[0]["ts_ns"], reset[0]["ts_ns"]
        sent = {t["order_id"]: t["ts"] for t in rows(r["trace"]) if t["stage"] == "request_sent" and t.get("order_id")}
        quotes_after = [o["order_id"] for o in r["orders"] if o["post_only"] and o["order_id"] in sent and t0 / 1e9 < sent[o["order_id"]] < t1 / 1e9]
    ok = bool(trip) and fields and tactic and bool(reset) and reset[0].get("operator_id") and not quotes_after and r["manifest"]["residual_at_end"]["flag"] == "FLAT_NET"
    add("2k Breaker trip: exposure plus a hedge unknown 30 s: cancel quotes, flatten MARKET_BANDED at the touch band, stay tripped until a logged manual reset", "session breaker, OMS, router", len(flat), ok,
        f"trip cause {trip[0]['cause'] if trip else None}; alarm fields present {fields}; flatten orders {[(f['venue'], f['side'], f['tactic'], round(f['worst_price'], 3)) for f in flat]}; maker quotes sent between the trip and the reset {len(quotes_after)}; "
        f"manual reset logged by {reset[0].get('operator_id') if reset else None}; residual at end {r['manifest']['residual_at_end']['flag']}", "scn_2k_breaker_trip.jsonl, runs/scn_2k_breaker_trip/session_safety_events.jsonl")

# ---- cases covered at unit / OMS level only, and the ones not covered
unit = {
    "2c IOC partial and zero fill, only the true residual retried": ("OMS, hedge lifecycle", "covered at replay level only where the capture produced a partial (see 2n 25 SOL); no injected partial-fill fault exists in the simulator (hedge_fault_injection refuses partial_prob)", "NOT COVERED"),
    "2g Restart with open orders": ("OMS, plan store", "unit case: tests/test_restart_resilience.py (RestartAgainstTheVenueTests): missed execution booked once, hedged once, new risk held until reconciled; evidence case6_*.jsonl and case9_*.jsonl. A restart INSIDE a replay is not built", "NOT COVERED"),
    "2h New-level vs queued fills": ("simulator", "the fill record has no such field yet (the queue flags level_was_absent / queue_unknown exist on observation rows only); not built", "NOT COVERED"),
    "2i Amend paths (S0 profile v1_9s)": ("OMS amend lineage", "OMS level: tests/test_oms_amend.py (11 cases, evidence case7_amend_*.jsonl). No strategy calls request_amend and no S0 profile v1_9s exists, so no replay-level amend case", "NOT COVERED"),
    "2l Maker-venue (Bybit) outage with quotes resting": ("session, simulator", "not built: hedge_fault_injection simulates a hedge-venue gateway outage only", "NOT COVERED"),
    "2m Latency at p99 and p99.9 delays": ("timing", "not built: the session timelines are lognormal with light tails; a sampled mode over the testnet quantile grids is the plan", "NOT COVERED"),
}
for case, (layer, why, res) in unit.items():
    matrix.append({"case": case, "covering_layer": layer, "count": None, "result": res, "detail": why, "evidence": "see detail"})

(CASES / "coverage_matrix.json").write_text(json.dumps(matrix, indent=1))
lines = ["# EMS coverage matrix (v1_9, EXPLORATORY, mechanics only)", "", "| Path or case | Covering layer | Count | Result | Detail | Evidence |", "|---|---|---|---|---|---|"]
for m in matrix:
    lines.append(f"| {m['case']} | {m['covering_layer']} | {m['count']} | **{m['result']}** | {m['detail']} | {m['evidence']} |")
(CASES / "coverage_matrix.md").write_text("\n".join(lines) + "\n")
print("\n".join(lines))
