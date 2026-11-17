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


ATTR = DAY / "attribution"                       # owner 13 Nov: the same cases with the layer gate in enforce and NO breaker, to show what the breaker (the profile's own value) changed
VARIANTS = (("", RUNS, ""), (" [attribution: layer gate enforce, breaker OFF]", ATTR, "_layeronly"))


def load(case, base=None, sfx=""):
    d = (base or RUNS) / f"scn_{case}{sfx}"
    if not (d / "session_manifest.json").exists():
        return None
    m = json.loads((d / "session_manifest.json").read_text())
    return {"dir": d, "manifest": m, "orders": rows(d / "session_orders.jsonl"), "fills": rows(d / "session_fills.jsonl"), "signals": rows(d / "session_signals.jsonl"),
            "cov": json.loads((d / "session_ems_path_coverage.json").read_text()), "life": json.loads((d / "session_lifecycle_report.json").read_text()),
            "pairs": rows(d / "session_pair_economics.jsonl") if (d / "session_pair_economics.jsonl").exists() else [], "trace": (d if sfx else CASES) / f"scn_{case}.jsonl"}


def hedge_orders(r):
    return [o for o in r["orders"] if o["venue"] == "BINANCE" and o["state"] != "CREATED"]


def maker_fills(r):
    return [f for f in r["fills"] if f["venue"] == "BYBIT"]


def common_checks(r):
    c = r["cov"]
    return c["checks"]["pass"], f"off_tick={len(c['checks']['off_tick'])}, tactic_violations={len(c['tactic_violations'])}"


matrix = []


def add(case, layer, count, ok, detail, evidence):
    if ok is False and case.startswith("2c Zero fill") and "[attribution" not in case:
        detail += " || WHY IT FAILS (owner 13 Nov, the profile's own safety values): the profile's circuit breaker is ON in this run and tripped (runs/scn_2c_*/session_safety_events.jsonl: BREAKER_TRIP, flatten, kill switch), which stops the plans this rule counts. The same case with the layer gate in enforce and the breaker OFF is the attribution row below (it passes): the layer gate itself changed nothing. Reported as a finding, not tuned away"
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
def hedge_chain_check(r):
    """(retry_ok, total hedged == total maker qty, n zero, n partial): every unfilled or partly filled hedge IOC is followed, later, by an order on the same side for exactly its true residual."""
    h = [o for o in r["orders"] if o["venue"] == "BINANCE" and o["state"] != "CREATED"]
    ok, zero, part = True, 0, 0
    for i, o in enumerate(h):
        q, f = float(o["quantity"]), float(o["filled_quantity"])
        if f >= q - 1e-9:
            continue
        zero += f == 0
        part += f > 0
        ok = ok and any(abs(float(n["quantity"]) - (q - f)) < 1e-9 and n["side"] == o["side"] for n in h[i + 1:])
    qh, qm = sum(float(o["filled_quantity"]) for o in h), sum(float(o["filled_quantity"]) for o in r["orders"] if o["venue"] == "BYBIT")
    return ok, abs(qh - qm) < 1e-9, zero, part


def thin_injected(r):
    inj = (r["manifest"].get("manifest") or {}).get("hedge_thin_book") or r["manifest"].get("hedge_thin_book") or {}
    return sum(v for n, v in (inj.get("injected_by_venue") or {}).get("BINANCE", {}).items() if n.startswith("thin_book"))


r = load("2m_processing")
if r and base:
    ub = base["life"]["total"]["residual_exposure"]["unhedged_interval_s"]; ur = r["life"]["total"]["residual_exposure"]["unhedged_interval_s"]
    add("2m Latency: processing 5+5 ms on quote and hedge paths", "router, timing", ur.get("n", 0), ur.get("p50", 0) > ub.get("p50", 1e9),
        f"unhedged interval p50 {ub.get('p50')} -> {ur.get('p50')} s; races {r['cov']['paths']['fills_during_cancel_pending']}; the p99 and p99.9 runs are the next two rows", str(r["trace"].name))
for case, q in (("2m_p99", "p99"), ("2m_p999", "p99.9")):
    r = load(case)
    if r and base:
        u = r["life"]["total"]["residual_exposure"]["unhedged_interval_s"]; ub = base["life"]["total"]["residual_exposure"]["unhedged_interval_s"]
        paths, res = r["cov"]["paths"], r["manifest"]["residual_at_end"]
        retry_ok, balanced, zero, part = hedge_chain_check(r)
        qm = r["life"]["total"]["quotes"]
        tim = r["manifest"].get("manifest", {}).get("timing") or r["manifest"].get("timing") or {}
        ok = paths["kill_switch_transitions"] == 0 and paths["hedge_alarm"] == 0 and paths["unwind_exhausted_alarm"] == 0 and r["cov"]["checks"]["pass"] and abs(float(res["net_position_base"])) < 1.0 and res["open_residual_plans"] == 0
        add(f"2m Latency: every order delay fixed at the testnet {q} of its stage, both venues (feed unchanged)", "router, timing, OMS, hedge lifecycle", u.get("n", 0), ok,
            f"unhedged interval (fill report to hedge fill) p50 {ub.get('p50')} -> {u.get('p50')} s, max {ub.get('max')} -> {u.get('max')} s (baseline -> this run); fills during cancel pending {paths['fills_during_cancel_pending']}, late fills after cancel {paths['late_fills_after_cancel']}, "
            f"quotes placed {qm['quotes_placed']} filled {qm['quotes_filled']} (baseline {base['life']['total']['quotes']['quotes_placed']} / {base['life']['total']['quotes']['quotes_filled']}); hedge retries {paths['hedge_retry']}, unwind orders {paths['unwind_orders']}, kill switch transitions {paths['kill_switch_transitions']}; "
            f"hedged quantity equals maker quantity {balanced}; residual at end {res['flag']} ({res['net_position_base']} SOL). Delays: TESTNET empirical quantile grids (entry offset-corrected one-way, ack REST RTT, cancel = half the cancel REST RTT each for effect and report, fill report = the sample maximum, n=28); "
            f"the stage mapping is ASSUMED and the numbers are an indicative bound for mainnet, not a measurement of it. At p99.9 of n about 390 the value is effectively the sample maximum. A fixed delay at the quantile, not a sampled mixture (a sampled mode is not built)", str(r["trace"].name))
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

def safety(case, base=None, sfx=""):
    f = (base or RUNS) / f"scn_{case}{sfx}" / "session_safety_events.jsonl"
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
    tactic = all(f["tactic"] == "MARKET_BANDED" for f in flat)           # every flatten, if any, is MARKET_BANDED. Since 13 Nov (section N) the breaker flattens only what no open plan manages: an exposure whose hedge is in flight (here the ghost hedge) is closed by that hedge, so no flatten is required
    band_ok = True
    r = load("2k_breaker_trip")
    quotes_after = []
    if trip and reset:
        t0, t1 = trip[0]["ts_ns"], reset[0]["ts_ns"]
        sent = {t["order_id"]: t["ts"] for t in rows(r["trace"]) if t["stage"] == "request_sent" and t.get("order_id")}
        quotes_after = [o["order_id"] for o in r["orders"] if o["post_only"] and o["order_id"] in sent and t0 / 1e9 < sent[o["order_id"]] < t1 / 1e9]
    kind = lambda o: str(o.get("intent_id") or "-").split("-")[-1][:1]
    exposed = sum(float(o["filled_quantity"]) for o in r["orders"] if kind(o) == "P") - sum(float(o["filled_quantity"]) for o in r["orders"] if kind(o) == "H")
    closed_once = sum(float(o["filled_quantity"]) for o in r["orders"] if kind(o) == "U") <= exposed + 1e-9 and abs(float(r["manifest"]["residual_at_end"]["net_position_base"])) < 0.01
    ok = bool(trip) and fields and tactic and bool(reset) and reset[0].get("operator_id") and not quotes_after and r["manifest"]["residual_at_end"]["flag"] == "FLAT_NET" and closed_once
    add("2k Breaker trip: exposure plus a hedge unknown 30 s: cancel quotes, the exposure closed once (the hedge in flight, or a MARKET_BANDED flatten at the touch band for what no open plan manages), stay tripped until a logged manual reset", "session breaker, OMS, router", len(flat), ok,
        f"trip cause {trip[0]['cause'] if trip else None}; alarm fields present {fields}; flatten orders {[(f['venue'], f['side'], f['tactic'], round(f['worst_price'], 3)) for f in flat]}; maker quotes sent between the trip and the reset {len(quotes_after)}; "
        f"manual reset logged by {reset[0].get('operator_id') if reset else None}; residual at end {r['manifest']['residual_at_end']['flag']}", "scn_2k_breaker_trip.jsonl, runs/scn_2k_breaker_trip/session_safety_events.jsonl")

ev = safety("2l_maker_outage")
if ev is not None:
    r = load("2l_maker_outage")
    DAY0 = 1793232000
    def hms(x):
        a = [float(v) for v in x.split(":")] + [0, 0]
        return DAY0 + a[0] * 3600 + a[1] * 60 + a[2]
    wins = [(hms(a), hms(b)) for a, b in r["manifest"]["run"]["settings"]["maker_outage"]]
    mf = maker_fills(r)
    inside = [f for f in mf if any(a <= f["ts_ns"] / 1e9 - 1 and f["ts_ns"] / 1e9 <= b + 1 for a, b in wins)]
    booked_after = [f for f in mf if f["ts_ns"] / 1e9 >= wins[0][1]]
    hedges = [f for f in r["fills"] if f["venue"] == "BINANCE"]
    first_maker, hedge_after = (booked_after[0]["ts_ns"] if booked_after else None), [h["ts_ns"] for h in hedges if booked_after and h["ts_ns"] >= booked_after[0]["ts_ns"]]
    lag_ms = round((hedge_after[0] - first_maker) / 1e6, 1) if hedge_after else None
    qty_m, qty_h = round(sum(float(f["fill_quantity"]) for f in mf), 6), round(sum(float(f["fill_quantity"]) for f in hedges), 6)
    timed = [o for o in r["orders"] if o["state"] == "TIMED_OUT"]
    kills = [e for e in ev if e["event_type"] == "KILL_SWITCH_STATE_TRANSITION" and e.get("action") == "ACTIVATED"]
    queries = [x for x in rows(r["dir"] / "session_decision_events.jsonl") if False]
    ok = bool(booked_after) and booked_after[0]["ts_ns"] / 1e9 >= wins[0][1] and lag_ms is not None and lag_ms < 1000 and qty_m == qty_h and len(timed) == 1 and bool(kills) and r["manifest"]["residual_at_end"]["flag"] == "FLAT_NET"
    add("2l Maker-venue (Bybit) outage with quotes resting: a quote resting at the outage fills inside it; a quote due inside it never reaches the venue", "session, simulator, OMS",
        len(wins), ok,
        f"outages {[(a - DAY0, b - DAY0) for a, b in wins]} s of the day; fills inside the first window booked at the reconnect {round(booked_after[0]['ts_ns'] / 1e9 - wins[0][1], 3) if booked_after else None} s after it ended; hedge {lag_ms} ms after the booked fill; maker qty {qty_m} = hedge qty {qty_h}; "
        f"lost-submit quote ended TIMED_OUT after 3 unanswered queries (never resent): {len(timed)}; kill switch 'unconfirmed order state' activated: {len(kills)} (stays on until a logged manual reset); residual at end {r['manifest']['residual_at_end']['flag']}. "
        f"Needs oms.maker_ack_deadline (off by default; without it the lost quote sat SENT 565 s, recorded). NOT simulated: a submit that reached the venue with only its ack lost, and the stream reconnect replaying events",
        "scn_2l_maker_outage.jsonl, runs/scn_2l_maker_outage/session_safety_events.jsonl")

LOT = 0.01                                              # the hedge venue's (Binance) SOLUSDT lot: the silent-carry bound of v1_9 (hedge_faults.silent_carry_bound: below_one_lot, owner 13 Nov)


def pre_trip_orders(d, r):
    """The orders of the plans that had finished before the BREAKER_TRIP (all their orders terminal before it): the old retry mechanics are checked on these only."""
    ev = rows(d / "session_safety_events.jsonl") if (d / "session_safety_events.jsonl").exists() else []
    trip_ts = next((int(e["ts_ns"]) for e in ev if e["event_type"] == "BREAKER_TRIP"), None)
    plan = lambda o: str(o.get("intent_id") or "-").split("-")[0]
    done = {}
    for o in r["orders"]:
        t = o.get("terminal_ts_ns")
        done[plan(o)] = done.get(plan(o), True) and (trip_ts is None or (t is not None and int(t) < trip_ts))
    return [o for o in r["orders"] if done.get(plan(o)) and o["venue"] in ("BINANCE", "BYBIT")]


def breaker_design(d, r):
    """The 2c pass rule under the profile's own breaker (v1_9_TOLERANCES.md section N, written before the rerun): (ok, text). Every part is measured from the run's files."""
    ev = rows(d / "session_safety_events.jsonl") if (d / "session_safety_events.jsonl").exists() else []
    trips = [e for e in ev if e["event_type"] == "BREAKER_TRIP"]
    trip_ok = bool(trips) and "consecutive_failures" in str(trips[0].get("cause", ""))
    payload = ("execution_blocked_reason", "confirmed_residual_base", "pending_hedge_qty", "oldest_exposure_age_s")
    states = [e["state"] for e in ev if e["event_type"] == "BREAKER_STATE_TRANSITION"]
    alarm_ok = trip_ok and all(k in trips[0] for k in payload) and trips[0]["execution_blocked_reason"] == "breaker:TRIPPED" and states[-1:] == ["TRIPPED"] and not any(e["event_type"] == "BREAKER_RESET" for e in ev)
    kind = lambda o: str(o.get("intent_id") or "-").split("-")[-1][:1]
    maker = sum(float(o["filled_quantity"]) for o in r["orders"] if kind(o) == "P")
    hedged = sum(float(o["filled_quantity"]) for o in r["orders"] if kind(o) == "H")
    unwound = sum(float(o["filled_quantity"]) for o in r["orders"] if kind(o) == "U")
    once_ok = unwound <= maker - hedged + 1e-9
    res = r["manifest"]["residual_at_end"]
    net = abs(float(res["net_position_base"]))
    lot_ok = net < LOT and res["open_residual_plans"] == 0
    acct_ok = abs(float(res["total_residual_base"]) - net) < 1e-9
    ok = trip_ok and alarm_ok and once_ok and lot_ok and acct_ok
    text = (f"trip {trip_ok} (cause {trips[0]['cause'] if trips else None}); alarm payload and latched {alarm_ok}; closed once {once_ok} (unwind filled {unwound:.4f} <= maker {maker:.4f} - hedged {hedged:.4f}); "
            f"end |net| {net:.6f} < one lot {LOT} and no open plan {lot_ok}; residual accounting {res['total_residual_base']} vs |net| {net:.12f} agree {acct_ok}")
    return ok, text


# ---- 2c: partial and zero IOC forced by a thinned hedge-venue book (opt-in, shows less than the capture)
for (case, title), (tag, base, sfx) in [(c, v) for c in (("2c_zero_then_fill", "2c Zero fill: the first hedge IOC of every plan finds nothing inside its limit, the retry fills"), ("2c_partial_then_fill", "2c Partial fill: the first hedge IOC finds 0.8 of 1.0, only the true residual is retried")) for v in VARIANTS]:
    d = base / f"scn_{case}{sfx}"
    if d.exists():
        r = load(case, base, sfx)
        retry_ok, balanced, zero, part = hedge_chain_check(r)
        paths = r["cov"]["paths"]
        want = zero if "zero" in case else part
        ok = retry_ok and balanced and want > 0 and paths["unwind_orders"] == 0 and paths["hedge_alarm"] == 0 and r["manifest"]["residual_at_end"]["flag"] == "FLAT_NET" and r["cov"]["checks"]["pass"]
        design_text = ""
        if not tag and "zero" in case:                                  # section N: the profile's own breaker is on, so the rule is the intended breaker design, not "no unwinds"
            ok_design, design_text = breaker_design(d, r)
            pre = pre_trip_orders(d, r)
            pre_retry_ok, _, pre_zero, _ = hedge_chain_check({"orders": pre})
            ok = ok_design and pre_retry_ok and pre_zero > 0 and r["cov"]["checks"]["pass"]
            design_text = f"INTENDED DESIGN (section N): {design_text}; plans finished before the trip: every unfilled hedge remainder retried once for its true residual {pre_retry_ok} ({pre_zero} zero fills); per-order checks pass {r['cov']['checks']['pass']}. "
        add(title + tag, "hedge lifecycle, OMS, simulator (thinned hedge book, opt-in)", want, ok,
            design_text + f"thinned attempts injected {thin_injected(r)}; hedge IOC full {paths['hedge_ioc_full']}, partial {paths['hedge_ioc_partial']}, zero {paths['hedge_ioc_zero']}, retries {paths['hedge_retry']}; every unfilled remainder retried exactly once for its true residual {retry_ok}; "
            f"hedged quantity equals maker quantity {balanced}; unwind orders {paths['unwind_orders']}, alarms {paths['hedge_alarm']}; residual at end {r['manifest']['residual_at_end']['flag']}. The OMS priced the hedge from ITS full view of the book; the venue-truth book had less (no depth is added)", f"scn_{case}.jsonl")


def plan_unwinds(r):
    """Per plan (intent id sigN): the maker fill, the failed hedge attempts, and the unwinds that followed, with the unwind price against the maker price (loss in bps, positive = worse)."""
    out = {}
    for o in r["orders"]:
        iid = str(o.get("intent_id") or "")
        if not iid.startswith("sig"):
            continue
        sig, kind = iid.split("-")[0], iid.split("-")[1][:1]
        out.setdefault(sig, {"P": [], "H": [], "U": []}).setdefault(kind, []).append(o)
    rep = []
    for sig, k in out.items():
        if not k["U"]:
            continue
        mk = [o for o in k["P"] if float(o["filled_quantity"]) > 0]
        mq = sum(float(o["filled_quantity"]) for o in mk)
        mp = sum(float(o["filled_quantity"]) * float(o["average_price"]) for o in mk) / mq if mq else None
        for u in k["U"]:
            uq, up = float(u["filled_quantity"]), float(u["average_price"] or 0)
            loss = None if not (mp and uq) else (((mp - up) if u["side"] == "SELL" else (up - mp)) / mp) * 1e4
            rep.append({"plan": sig, "maker_filled": mq, "hedge_attempts": len(k["H"]), "hedged": sum(float(h["filled_quantity"]) for h in k["H"]), "unwind_qty": float(u["quantity"]), "unwind_filled": uq, "unwind_state": u["state"], "loss_bps": loss})
    return rep


for tag, base, sfx in VARIANTS:
    d = base / f"scn_2c_zero_exhausted{sfx}"
    if d.exists():
        r = load("2c_zero_exhausted", base, sfx)
        paths, ev3 = r["cov"]["paths"], safety("2c_zero_exhausted", base, sfx) or []
        res, cn = r["manifest"]["residual_at_end"], r["manifest"].get("ledger_event_counts", {})
        uw = plan_unwinds(r)
        failed = [x for x in uw if x["hedge_attempts"] >= 3 and x["hedged"] == 0]
        sub = [x for x in uw if x["unwind_filled"] < x["unwind_qty"]]
        carried = float(res["total_residual_base"])
        ok = (paths["hedge_ioc_zero"] > 0 and paths["hedge_retry"] > 0 and bool(failed) and all(x["unwind_filled"] >= x["unwind_qty"] - 1e-9 for x in failed) and carried < LOT
              and res["open_residual_plans"] == 0 and cn.get("RESIDUAL_WITHIN_APPROVED_BOUND", 0) <= 1 and r["cov"]["checks"]["pass"])
        losses = [round(x["loss_bps"], 2) for x in failed if x["loss_bps"] is not None]
        design_text = ""
        if not tag:                                                    # section N: the profile's own breaker is on: the intended design (the sig1 plan of the first failures unwinds in full; the rest is the breaker design)
            ok_design, design_text = breaker_design(d, r)
            ok = ok_design and bool(failed) and all(x["unwind_filled"] >= x["unwind_qty"] - 1e-9 for x in failed) and paths["hedge_ioc_zero"] > 0 and r["cov"]["checks"]["pass"]
            design_text = f"INTENDED DESIGN (section N): {design_text}; plans whose hedge failed on every attempt were unwound in full {bool(failed) and all(x['unwind_filled'] >= x['unwind_qty'] - 1e-9 for x in failed)}. "
        add("2c Zero fill on every attempt: retries run out, the residual is unwound at the 15 bps band whatever its size, only a sub-lot remainder is carried" + tag, "hedge lifecycle, OMS unwind, simulator (thinned hedge book, opt-in)", paths["hedge_ioc_zero"], ok,
            design_text + f"hedge IOC zero {paths['hedge_ioc_zero']}, retries {paths['hedge_retry']}, unwind intents {paths['unwind_intents']}, unwind orders {paths['unwind_orders']}, hedge alarms {paths['hedge_alarm']}, unwind-exhausted alarms {paths['unwind_exhausted_alarm']}; "
            f"plans whose hedge failed on all attempts and were unwound: {[(x['plan'], x['maker_filled'], x['unwind_filled'], x['loss_bps'] if x['loss_bps'] is None else round(x['loss_bps'], 2)) for x in failed]} (plan, maker filled, unwind filled, loss against the maker price in bps); "
            f"residual at end {res['flag']} ({res['total_residual_base']} SOL carried, below one lot of {LOT}; open plans {res['open_residual_plans']}); events RESIDUAL_WITHIN_APPROVED_BOUND {cn.get('RESIDUAL_WITHIN_APPROVED_BOUND', 0)} (the sub-lot remainder only), RESIDUAL_LIMIT_BREACH {cn.get('RESIDUAL_LIMIT_BREACH', 0)}. "
            f"Owner 13 Nov: a failed hedge always unwinds (MARKET_BANDED at the touch band) whatever its size; v1_9 sets the silent-carry bound to one lot. Before the change the 0.0876 SOL plan of this case was carried to the end of the day with no action", "scn_2c_zero_exhausted.jsonl")


# ---- 2c extra (owner 13 Nov): only the Binance TOUCH is thin, the hedge is larger than it, the size_aware cap spans levels
d = RUNS / "scn_2c_thin_top"
if d.exists():
    r = load("2c_thin_top")
    top = r["manifest"]["run"]["settings"]["thin_view_top"]
    dec = rows(d / "session_decision_events.jsonl")
    caps = [e for e in dec if e.get("event_type") == "HEDGE_CAP_SIZE_AWARE" or "cap" in e and "touch_covers" in e]
    spans = [e for e in caps if not e["touch_covers"]]
    used, ords = set(), [o for o in r["orders"] if o["venue"] == "BINANCE" and o["state"] != "CREATED" and "-U" not in str(o.get("intent_id"))]
    rowsx = []
    for o in ords:
        for i, e in enumerate(caps):
            if i not in used and e["cap"] == o["limit_price"] and float(e["qty"]) == float(o["quantity"]):
                used.add(i)
                touch, fq = float(e["touch"]), float(o["filled_quantity"])
                if fq > 0:
                    avg = float(o["average_price"])
                    slip = ((avg - touch) if o["side"] == "BUY" else (touch - avg)) / touch * 1e4
                else:
                    slip = None
                capbps = abs(float(e["cap"]) - touch) / touch * 1e4
                rowsx.append({"qty": float(e["qty"]), "touch_shown": top, "cap": float(e["cap"]), "cap_bps_from_touch": capbps, "levels_to_reach": e.get("deepest"), "covered": float(e["covered_qty"]), "filled": fq, "slip_bps": slip, "binds": e.get("abs_limit_binds")})
                break
    sp = [x for x in rowsx if x["qty"] > top]
    filled = [x for x in sp if x["filled"] > 0]
    retry_ok, balanced, zero, part = hedge_chain_check(r)
    ok = bool(spans) and bool(filled) and all(x["cap_bps_from_touch"] <= 15.0 + 1e-6 for x in rowsx) and balanced and retry_ok and r["cov"]["checks"]["pass"]
    sl = [x["slip_bps"] for x in filled if x["slip_bps"] is not None]
    cp = [x["cap_bps_from_touch"] for x in sp]
    add(f"2c Thin touch, deeper levels intact: the Binance touch shows {top} SOL (to the bot and at the venue), the hedge is larger, the size_aware cap spans levels", "OMS size_aware cap, simulator (thinned touch, opt-in)", len(sp), ok,
        f"hedge attempts {len(rowsx)}; the touch did not cover the size in {len(spans)} cap decisions; attempts larger than the touch {len(sp)}, of which filled {len(filled)} (full {sum(1 for x in filled if abs(x['filled'] - x['qty']) < 1e-9)}, partial {sum(1 for x in filled if abs(x['filled'] - x['qty']) >= 1e-9)}); "
        f"cap distance from the touch bps min/median/max {min(cp):.2f}/{sorted(cp)[len(cp) // 2]:.2f}/{max(cp):.2f} (limit 15; absolute limit bound in {sum(1 for x in sp if x['binds'])}); fill slippage against the decision touch bps min/median/max {min(sl):.2f}/{sorted(sl)[len(sl) // 2]:.2f}/{max(sl):.2f}; "
        f"hedged quantity equals maker quantity {balanced}; zero fills {zero}, partial {part}, every remainder retried for its true residual {retry_ok}; residual at end {r['manifest']['residual_at_end']['flag']}. "
        f"The touch is thinned for the bot's cap AND at the venue; deeper levels are the capture's, nothing is added" if sl and cp else "no hedge larger than the touch in this window", f"scn_2c_thin_top.jsonl")

# ---- 2h: the queue class of every maker fill (observation rows)
cnt, tot, rows_seen = {}, 0, []
for d in sorted(RUNS.glob("scn_*")):
    f = d / "maker_observations.jsonl"
    if f.exists():
        mf = [x for x in rows(f) if x.get("obs") == "maker_fill" and "queue_class" in x]
        if mf:
            rows_seen.append(d.name)
            for x in mf:
                cnt[x["queue_class"]] = cnt.get(x["queue_class"], 0) + 1
                tot += 1
if tot:
    add("2h New-level vs queued fills: every maker fill carries queue_class", "simulator observation rows", tot, True,
        f"maker fills with the field {tot} in {len(rows_seen)} runs ({', '.join(rows_seen)}): {cnt}. The class is read from the placement of the same order (new_level: no displayed size at our price when we placed, queued: behind displayed size, unknown: queue not known); "
        f"it sits on the maker_fill rows of maker_observations.jsonl, an observation-only file, so no existing output changed. Agreement with real fills is a Track C result and is NOT claimed", "runs/scn_*/maker_observations.jsonl")

# ---- v1_10 reruns (owner 16 Nov, Order 2): cases 2a, 2b, 2f, 2m, 2n on v1_10 (withdrawal + S0 + dust carry + 1 s no-requote + the 5 s mark rule). The same rules as the v1_9 rows; a case whose event never happened is NOT COVERED, never PASS.
def v110_row(case, title, layer, event_count, rule_ok, detail, evidence):
    if rule_ok is False:
        add(title, layer, event_count, False, detail, evidence)
    elif not event_count:
        add(title, layer, event_count, None, detail + " THE EVENT THIS CASE IS ABOUT DID NOT HAPPEN IN THIS RUN: NOT COVERED, not PASS (rule 6).", evidence)
    else:
        add(title, layer, event_count, True, detail, evidence)


def v110_common(r):
    res = r["manifest"]["residual_at_end"]
    qm = sum(float(o["filled_quantity"]) for o in r["orders"] if o["venue"] == "BYBIT"); qh = sum(float(o["filled_quantity"]) for o in r["orders"] if o["venue"] == "BINANCE")
    dust = (r["manifest"].get("manifest", {}).get("hedge_dust_carry") or {})
    return res, qm, qh, abs(qm - qh) < 1e-9 or res.get("flag") in ("FLAT_NET",) and abs(float(res.get("net_position_base", 0))) < 1e-9


r = load("2a_v110")
if r:
    c = r["cov"]["paths"]; res, qm, qh, bal = v110_common(r)
    withdrawn = [o for o in r["orders"] if (o.get("cancel_reason") or "").startswith("quote_withdrawn")]
    v110_row("2a_v110", "2a v1_10: Binance move before the timer tick (quote_withdraw on)", "strategy, OMS, router, simulator", c["maker_withdrawn_on_event"], r["cov"]["checks"]["pass"] and len(withdrawn) <= max(c["maker_withdrawn_on_event"], 0) and bal,
             f"withdrawn on the event {c['maker_withdrawn_on_event']}, order rows with the reason {len(withdrawn)}, fills during cancel pending {c['fills_during_cancel_pending']}; maker filled {qm:g} hedge filled {qh:g}; residual at end {res['flag']}", str(r["dir"].name))
r = load("2b_v110")
if r:
    c = r["cov"]["paths"]; ids = [f["fill_id"] for f in r["fills"]]; res, qm, qh, bal = v110_common(r)
    v110_row("2b_v110", "2b v1_10: fill during cancel pending (cancel latency 800 ms); late fill after the cancel", "OMS, simulator", c["fills_during_cancel_pending"] + c["late_fills_after_cancel"],
             len(ids) == len(set(ids)) and bal and r["cov"]["checks"]["pass"], f"during cancel pending {c['fills_during_cancel_pending']}, late after cancel {c['late_fills_after_cancel']}; fill ids unique {len(ids) == len(set(ids))}; maker filled {qm:g} hedge filled {qh:g}; residual at end {res['flag']}", str(r["dir"].name))
r = load("2f_v110")
if r:
    c = r["cov"]["paths"]; res, qm, qh, bal = v110_common(r)
    refused_h = sum(1 for o in r["orders"] if o["venue"] == "BINANCE" and o.get("cancel_reason") == "scoped_rate_limited")
    skipped = r["manifest"].get("ledger_event_counts", {}).get("QUOTE_SKIPPED_RATE_LIMIT", 0)
    v110_row("2f_v110", "2f v1_10: rate-limit pressure (6 requests / 10 s; quote share 80%, 2 always free)", "router, rate limiter, session", c["rate_limit_deferral"] + skipped, refused_h == 0 and bal and res["flag"] == "FLAT_NET",
             f"hedges refused {refused_h} (rule: 0); quotes skipped before a plan existed {skipped}; queued dispatches {c['rate_limit_deferral']}; maker filled {qm:g} hedge filled {qh:g}; residual at end {res['flag']}", str(r["dir"].name))
for case, q in (("2m_processing_v110", "processing 5+5 ms"), ("2m_p99_v110", "p99"), ("2m_p999_v110", "p99.9")):
    r = load(case)
    if r:
        paths = r["cov"]["paths"]; res, qm, qh, bal = v110_common(r); u = r["life"]["total"]["residual_exposure"]["unhedged_interval_s"]
        ok = paths["kill_switch_transitions"] == 0 and paths["hedge_alarm"] == 0 and paths["unwind_exhausted_alarm"] == 0 and r["cov"]["checks"]["pass"] and abs(float(res["net_position_base"])) < 1.0 and res["open_residual_plans"] == 0 and bal
        v110_row(case, f"2m v1_10: latency {q}", "router, timing, OMS, hedge lifecycle", u.get("n", 0), ok,
                 f"unhedged interval p50 {u.get('p50')} s, max {u.get('max')} s (n {u.get('n', 0)}); fills during cancel pending {paths['fills_during_cancel_pending']}, late fills {paths['late_fills_after_cancel']}, hedge retries {paths['hedge_retry']}, unwind orders {paths['unwind_orders']}, kill switch transitions {paths['kill_switch_transitions']}; maker filled {qm:g} hedge filled {qh:g}; residual {res['flag']}", str(r["dir"].name))
for sz in ("1", "5", "25"):
    r = load(f"2n_size{sz}_v110")
    if r:
        h = hedge_orders(r); res, qm, qh, bal = v110_common(r); paths = r["cov"]["paths"]
        v110_row(f"2n_size{sz}_v110", f"2n v1_10: size {sz} SOL (size_aware cap, participation cap on)", "OMS, size_aware cap, simulator, E3", len(h), r["cov"]["checks"]["pass"] and bal,
                 f"hedge IOC full {paths['hedge_ioc_full']}, partial {paths['hedge_ioc_partial']}, zero {paths['hedge_ioc_zero']}, unwind orders {paths['unwind_orders']}; maker filled {qm:g} hedge filled {qh:g}; residual at end {res['flag']}; withdrawn {paths['maker_withdrawn_on_event']}", str(r["dir"].name))

# ---- Order 3 limits on v1_10 (owner 16 Nov). Each case INJECTS its condition (labelled); a limit that never bound is NOT COVERED, never PASS. Baseline = the same dense window with every limit at the profile's own value.
def _limits_of(case):
    f = RUNS / f"scn_{case}" / "session_limits.json"
    return json.loads(f.read_text()) if f.exists() else None


def _o3(case, title, layer, bound, ok, detail, evidence=None):
    if ok is False:
        add(title, layer, bound, False, detail, evidence or f"runs/scn_{case}/session_limits.json")
    elif not bound:
        add(title, layer, bound, None, detail + " THE LIMIT NEVER BOUND IN THIS RUN: NOT COVERED, not PASS.", evidence or f"runs/scn_{case}/session_limits.json")
    else:
        add(title, layer, bound, True, detail, evidence or f"runs/scn_{case}/session_limits.json")


def _filled_pair(r):
    qm = sum(float(o["filled_quantity"]) for o in r["orders"] if o["venue"] == "BYBIT"); qh = sum(float(o["filled_quantity"]) for o in r["orders"] if o["venue"] == "BINANCE")
    return qm, qh


base3, lim3 = load("3x_baseline_v110"), _limits_of("3x_baseline_v110")
if base3 and lim3:
    bound = {k: v for k, v in lim3["counters"].items() if k != "clock_not_evaluable"}
    add("3x Baseline (dense window, v1_10 with every Order 3 limit at the profile's value)", "session limits", len(bound), len(bound) == 0 and not lim3["kills"],
        f"signals {len(base3['signals'])}, maker orders {sum(1 for o in base3['orders'] if o['venue'] == 'BYBIT')}, fills {len(base3['fills'])}; no limit bound ({bound or 'none'}); the clock gate was 'not evaluable' {lim3['counters'].get('clock_not_evaluable', 0)} times (counted, not blocking); the daily P&L low point {lim3['daily_loss']['min_pnl_usdt'][:8]} USDT against the limit {lim3['daily_loss']['limit_usdt']}: the -5 USDT stop does not bind on this day at 1 SOL; the order rate peaked at {lim3['order_rate']['BYBIT']['taken']} orders taken, {lim3['order_rate']['BYBIT']['skipped']} skipped",
        "runs/scn_3x_baseline_v110")
r, lim = load("3a_order_rate_v110"), _limits_of("3a_order_rate_v110")
if r and lim and base3:
    qm, qh = _filled_pair(r); st = [e for e in lim["state_changes"] if e["limit"].startswith("order_rate")]
    skipped = lim["order_rate"]["BYBIT"]["skipped"]
    _o3("3a_order_rate_v110", "3a A1 order rate (INJECTED: 0.2 per second, burst 1, instead of 2 and 5)", "session, rate_limits.OrderRateBucket", skipped,
        r["cov"]["checks"]["pass"] and abs(qm - qh) < 1e-9 and sum(1 for e in st if e["state"] == "ACTIVE") == lim["counters"].get("order_rate:BYBIT_episodes", 0),
        f"skipped {skipped} (quotes {lim['counters'].get('order_rate_skipped_quote', 0)}, amends {lim['counters'].get('order_rate_skipped_amend', 0)}) of {lim['order_rate']['BYBIT']['taken'] + skipped} asks; {lim['counters'].get('order_rate:BYBIT_episodes', 0)} state changes recorded once each (ACTIVE) and cleared when a token returned; maker orders {sum(1 for o in r['orders'] if o['venue'] == 'BYBIT')} against {sum(1 for o in base3['orders'] if o['venue'] == 'BYBIT')} in the baseline; maker filled {qm:g} = hedge filled {qh:g}: hedges were never refused; nothing was queued")
r, lim = load("3b_loss_stop_v110"), _limits_of("3b_loss_stop_v110")
if r and lim:
    qm, qh = _filled_pair(r); act = [e for e in lim["state_changes"] if e["limit"] == "daily_loss_stop" and e["state"] == "ACTIVE"]
    res = r["manifest"]["residual_at_end"]
    _o3("3b_loss_stop_v110", "3b A2 daily loss stop (INJECTED: the limit set to -0.02 USDT so the day's own P&L trips it; the forced-adverse-fills variant is the unit test)", "session, OMS kill switch (keep_risk_reducing)", lim["counters"].get("daily_loss_stop_trips", 0),
        len(act) == 1 and abs(qm - qh) < 1e-9 and res["flag"] in ("FLAT_NET",) and abs(float(res["net_position_base"])) < 1e-9,
        f"tripped once at P&L {act[0]['pnl_usdt'][:9] if act else None} USDT; {len(act)} ACTIVE event; maker filled {qm:g} = hedge filled {qh:g} (the hedge in flight finished); residual {res['flag']}; orders after the trip: {len(r['orders'])} against {len(base3['orders']) if base3 else '?'} in the baseline (the kill is latched, no manual reset in a replay)")
rb, lb = load("3c_clock_bad_v110"), _limits_of("3c_clock_bad_v110")
if rb and lb:
    _o3("3c_clock_bad_v110", "3c A3 clock-offset gate (INJECTED measurement: uncertainty 6 ms against the 5 ms limit)", "session, time_integrity.ClockGate", lb["signals_blocked_by_limits"],
        len(rb["orders"]) == 0 and len([e for e in lb["state_changes"] if e["limit"] == "clock_offset"]) == 1,
        f"{lb['signals_blocked_by_limits']} signal evaluations blocked (reason clock_uncertain), maker orders {len(rb['orders'])}, one ACTIVE event; hedges and cancels are not asked (none were needed)")
ro, lo = load("3c_clock_ok_v110"), _limits_of("3c_clock_ok_v110")
rn, ln = load("3c_clock_none_v110"), _limits_of("3c_clock_none_v110")
if ro and lo and rn and ln and base3:
    same = [len(x["signals"]) for x in (ro, rn, base3)], [len(x["orders"]) for x in (ro, rn, base3)]
    add("3c A3 clock gate, measured 4 ms (INJECTED) and no measured clock", "session, time_integrity.ClockGate", ln["counters"].get("clock_not_evaluable", 0), same[0][0] == same[0][1] == same[0][2] and same[1][0] == same[1][1] == same[1][2] and lo["signals_blocked_by_limits"] == 0 and ln["signals_blocked_by_limits"] == 0,
        f"4 ms measured: evaluated 'ok', nothing blocked; no measured clock: 'not evaluable' {ln['counters'].get('clock_not_evaluable', 0)} times, counted and not blocking; signals and orders equal the baseline in both ({same[0][0]}, {same[1][0]})", "runs/scn_3c_clock_ok_v110, scn_3c_clock_none_v110")
r, lim = load("3d_margin_warning_v110"), _limits_of("3d_margin_warning_v110")
if r and lim:
    _o3("3d_margin_warning_v110", "3d A4 margin WARNING stop (INJECTED: 30 USDT capital per venue, so free margin is below 10x the initial margin at the hard cap)", "session, risk margin report", lim["signals_blocked_by_limits"],
        len(r["orders"]) == 0, f"{lim['signals_blocked_by_limits']} evaluations blocked (reason margin_warning, venue(s) {lim['state_changes'][0].get('venues') if lim['state_changes'] else None}), maker orders {len(r['orders'])}; one ACTIVE event; the 1000 USDT profile capital never triggers it (baseline)")
r, lim = load("3e_missing_marks_v110"), _limits_of("3e_missing_marks_v110")
if r and lim:
    kill = lim["kills"].get("missing_marks")
    _o3("3e_missing_marks_v110", "3e A5 missing marks (INJECTED: the Binance feed lost for 150 s, 11:20:00 to 11:22:30)", "session, OMS kill switch (keep_risk_reducing)", lim["counters"].get("missing_marks_trips", 0),
        kill is not None and kill["paused_s"] > 60.0 and lim["counters"].get("missing_marks_trips", 0) == 1,
        f"the price pause began at 11:20:05, the kill switch tripped once after {kill['paused_s']:.2f} s (limit 60 s); no hedge was in flight and the day's later signals were already over, so the stop on new quotes is shown by the unit test, not by this run; the price pause ended at the injected reconnect (11:22:30). (The first version of this case left the Binance book invalid for over an hour after the gap: an artefact of the injection, fixed 17 Nov by bridging the reconnect snapshot to the venue's delta chain)" if kill else "no trip")
r, lim = load("3g_close_pairs_on_v110"), None
if r and base3:
    c = r["manifest"]["ledger_event_counts"]
    fills_by = sorted((f for f in r["fills"] if f["venue"] == "BYBIT"), key=lambda f: f["ts_ns"])
    pos, lo, hi, closes, close_qty = 0.0, 0.0, 0.0, 0, 0.0
    for f in fills_by:
        q = float(f["fill_quantity"]); sell = f["side"] == "SELL"
        if sell and pos > 1e-9:
            closes += 1; close_qty += q
        pos += -q if sell else q
        lo, hi = min(lo, pos), max(hi, pos)
    bin_buy = sum(float(f["fill_quantity"]) for f in r["fills"] if f["venue"] == "BINANCE" and f["side"] == "BUY")
    bin_sell = sum(float(f["fill_quantity"]) for f in r["fills"] if f["venue"] == "BINANCE" and f["side"] == "SELL")
    maker_buy = sum(float(f["fill_quantity"]) for f in fills_by if f["side"] == "BUY"); maker_sell = sum(float(f["fill_quantity"]) for f in fills_by if f["side"] == "SELL")
    quotes = c.get("CLOSE_PAIR_QUOTE", 0)
    bad = c.get("CLOSE_PAIR_BEYOND_FLAT", 0) or lo < -1e-9
    carried = abs((maker_buy - maker_sell) + (bin_sell - bin_buy) * -1 - 0)         # unhedged remainder of maker fills (dust carried): maker net - hedge net
    gap = abs((maker_buy - maker_sell) - (bin_sell - bin_buy))
    _o3("3g_close_pairs_on_v110", "3g B pair close ON (arb.close_pairs reduce_only_quote, NOT in v1_10; the dense window of 3x with the switch on)", "strategy, OMS (reduce-only quote and hedge), session", closes,
        not bad and gap < 0.05 and r["cov"]["checks"]["pass"],
        f"closing quotes placed {quotes}, closing fills {closes} ({close_qty:g} SOL sold against longs on the maker venue); the maker venue position stayed between {lo:g} and {hi:g} (never below flat: never beyond flat, never opens a short); CLOSE_PAIR_BEYOND_FLAT {c.get('CLOSE_PAIR_BEYOND_FLAT', 0)}; hedge buys on the other venue {bin_buy:g} against closing fills {close_qty:g} (reduce-only), hedge sells {bin_sell:g} for maker buys {maker_buy:g}: hedged equals filled up to the dust carried ({gap:.4f} SOL, HEDGE_DUST_CARRIED {c.get('HEDGE_DUST_CARRIED', 0)}); signals {len(r['signals'])} against {len(base3['signals'])} with the switch off (3x), maker fills {len(fills_by)} against {sum(1 for f in base3['fills'] if f['venue'] == 'BYBIT')}")
# ---- W1 feed health on v1_11 (owner 17 Nov). 4x observation only; 4a / 4b the hedge feed lost with a quote resting (actions on for the case / off); 4c an injected feed delay with FEED_DELAYED on for the case.
def _fh(case):
    f = RUNS / f"scn_{case}" / "session_feed_health.json"
    return json.loads(f.read_text()) if f.exists() else None


def _quotes(r):
    j = {x["intent_id"]: x for x in rows(r["dir"] / "session_intent_journal.jsonl") if x.get("event") == "intent" and x.get("role") == "primary"}
    return [(j[o["intent_id"]]["created_ts"], o["terminal_ts_ns"] / 1e9, o) for o in r["orders"] if o.get("intent_id") in j and o.get("terminal_ts_ns")]


r, fh = load("4x_feed_health_v111"), _fh("4x_feed_health_v111")
if r and fh and base3:
    same = (len(r["signals"]), len(r["orders"]), len(r["fills"])) == (len(base3["signals"]), len(base3["orders"]), len(base3["fills"]))
    add("4x W1 feed health on v1_11, observation only (dense window)", "time_integrity.FeedHealthMonitor, session", fh["transitions"], same and fh["actions"] == {"block_new_quotes_on": "none", "withdraw_quotes_on": "none"},
        f"signals, orders and fills equal the v1_10 baseline 3x ({len(r['signals'])}, {len(r['orders'])}, {len(r['fills'])}): {same}; {fh['transitions']} state transitions recorded, one FEED_HEALTH_STATE event each ({fh['counters']}); "
        + "; ".join(f"{k.split('|')[0]} {k.split('|')[2]}: episodes {v['episodes']}, seconds DEGRADED {v['seconds_in_state']['DEGRADED']:.1f} UNSAFE {v['seconds_in_state']['UNSAFE']:.1f}" for k, v in fh["feeds"].items())
        + ". Thresholds ASSUMED; the capture's recorder quality is REJECTED: mechanics only", "runs/scn_4x_feed_health_v111/session_feed_health.json")
G0, G1 = 1793271600 + 93.0, 1793271600 + 133.0                # the injected loss, 11:01:33 to 11:02:13 UTC
for case, title in (("4a_hedge_feed_lost_actions_v111", "4a W1: hedge feed (Binance) lost 40 s with a maker quote resting; actions ON for the case (withdraw and block at DEGRADED; both none in v1_11)"),
                    ("4b_hedge_feed_lost_observe_v111", "4b W1: the same loss, observation only (v1_11 as it is): what happens today")):
    r, fh = load(case), _fh(case)
    if r and fh:
        resting = [(c, e, o) for c, e, o in _quotes(r) if c < G0 < e or G0 <= c < G0 + 1]
        ev = [e for e in rows(r["dir"] / "session_safety_events.jsonl") if e["event_type"] == "FEED_HEALTH_STATE" and e.get("venue") == "BINANCE" and e.get("stream") == "book" and G0 <= e["ts_ns"] / 1e9 <= G1 + 5]
        seq = [(e["previous"], e["state"]) for e in ev]
        live_after = [round(e - (G0 + 0.5), 2) for c, e, o in resting]            # seconds the quote stayed live after the feed was DEGRADED (0.5 s of silence)
        by_feed = [o.get("cancel_reason") for c, e, o in resting if str(o.get("cancel_reason") or "").startswith("quote_withdrawn:feed_health")]
        acting = case.startswith("4a")
        ok = seq == [("HEALTHY", "DEGRADED"), ("DEGRADED", "UNSAFE"), ("UNSAFE", "HEALTHY")] and bool(resting) and ((len(by_feed) == 1 and max(live_after) < 1.0) if acting else (not by_feed))
        _o3(case, title, "session feed health, OMS withdraw_quote", len(resting), ok,
            f"quotes resting when the feed was lost {len(resting)}; Binance book states in the window {seq} (one event each); withdrawn by feed health {len(by_feed)}; the quote stayed live {live_after} s after the DEGRADED point; "
            f"evaluations blocked by feed health {fh['counters'].get('signals_blocked_by_feed_health', 0)}; "
            + ("no quote was live after the transition" if acting else "WITHOUT the action the resting maker quote stayed live with the hedge feed dead (audit A1 1.4 gap 5, now on record); it ended by another rule, not by feed health"),
            f"runs/scn_{case}/session_feed_health.json")
r, fh = load("4c_feed_delay_v111"), _fh("4c_feed_delay_v111")
if r and fh:
    blocks = r["manifest"]["ledger_event_counts"].get("TIME_INTEGRITY_BLOCK", 0)
    ev = [e for e in rows(r["dir"] / "session_safety_events.jsonl") if e["event_type"] == "FEED_HEALTH_STATE" and e.get("venue") == "BINANCE" and e.get("cause") == "delay"]
    sent_in = [c for c, e, o in _quotes(r) if 1793271600 + 61.0 <= c < 1793271600 + 120.0]
    _o3("4c_feed_delay_v111", "4c W1 (W13-B1): INJECTED feed delay, Binance stamps 1.5 s late for 60 s, FEED_DELAYED on at 1000 ms FOR THE CASE (the threshold is PENDING the owner)", "time_integrity, session time gate", blocks,
        blocks > 0 and bool(ev) and not sent_in,
        f"events delayed by the injection {fh['injected_delay_events']}; TIME_INTEGRITY_BLOCK {blocks} (decisions blocked on those events and on the peer venue's); feed-health delay transitions on Binance {len(ev)} ({sorted({(e['previous'], e['state']) for e in ev})}); maker quotes sent inside the delayed minute {len(sent_in)} (rule: 0)")
add("3f A6 tier table and A8 account check at start-up (recorded messages)", "testnet transports, sdk, paper_live start-up", 21, True,
    "21 tests in tests/test_session_limits_startup.py: the Binance leverageBracket and Bybit risk-limit reads become ordered tiers (initial rate 1/leverage, deductions, order of brackets), an error or an unexpected shape raises, the read asks for the READ permission only, a spot transport says it has none; the start-up refuses when any venue's tiers cannot be read and replaces the ASSUMED tiers (source ACCOUNT_READ) when they can; verify_account refuses hedge mode, another margin mode or leverage, a missing leverage row, collects every problem, and labels reduce-only acceptance DERIVED_FROM_ONE_WAY_MODE and UNVERIFIED. HAND-WRITTEN messages in the documented shapes: the endpoint shapes are UNVERIFIED against the live endpoints, and the replays have no account (a replay keeps the ASSUMED tiers, labelled)", "tests/test_session_limits_startup.py")

# ---- cases covered at unit / OMS level only, and the ones not covered
def _case_2i():
    """Owner 14 Nov: S0 in the session runtime, rules of v1_9_TOLERANCES.md section O (written before the runs). Reads runs/scn_2i_* (session_s0_audit.json). A run with no amend request cannot pass the amend rules (rule 6)."""
    names = [n for n in ("2i_s0", "2i_s0_B", "2i_s0_reject", "2i_s0_timeout", "2i_s0_disconnect", "2i_s0_dup_filled") if (RUNS / f"scn_{n}" / "session_s0_audit.json").exists()]
    if not names:
        return ("OMS amend lineage + session controller", "no 2i run found (run scenarios.py 2i_s0)", "NOT COVERED")
    audits = {n: json.loads((RUNS / f"scn_{n}" / "session_s0_audit.json").read_text()) for n in names}
    amends = {n: a["amend_requests"] for n, a in audits.items()}
    bad = [n for n, a in audits.items() if a["two_live_orders"] or a["duplicate_fills_booked_twice"] or a["lineage_overfill"] or a["lineage_overfill_alarms"] or not a["hedged_equals_filled"]]
    held = (f"invariant rules 1-3 (two live orders 0, duplicate fills 0, lineage overfill 0, hedged = filled) hold in {len(names) - len(bad)} of {len(names)} runs"
            + (f"; BREAK in {bad}" if bad else ""))
    detail = f"amend requests per run: {amends}. {held}. Unit/OMS level: tests/test_oms_amend.py (11 lineage cases) and tests/test_s0_session.py (controller: amend, band, min interval, reject, timeout, no room, no target)."
    if bad:
        return ("session controller + OMS amend lineage", detail, "FAIL")
    if not any(amends.values()):
        return ("session controller + OMS amend lineage", detail + " NO amend request was made on the capture windows (rule 6: NOT COVERED, never PASS by emptiness): the arb's quotes rest on or near the touch, where a repricing target either sits inside the band or does not exist, so the amend path (paths a-j) is exercised by the synthetic stream tests only, not by the real capture. Reason recorded, not claimed.", "NOT COVERED")
    return ("session controller + OMS amend lineage", detail, "PASS")


def _case_2i_v110():
    """Owner 16 Nov, Order 2: case 2i on v1_10 with an INJECTED hedge-venue move on the real 29 Oct feed, and reject / timeout / disconnect / duplicate Filled acting on REAL amends. Reads runs/scn_2i_v110_* (session_s0_audit.json,
    session_injection.json). Never passes on emptiness: a variant whose fault did not meet an amend is NOT COVERED, and any broken invariant is FAIL."""
    variants = (("2i_v110_dense", "injected move only", lambda a, c: a["amend_requests"] >= 1),
                ("2i_v110_reject", "every amend rejected", lambda a, c: a["amends_rejected"] >= 1),
                ("2i_v110_timeout", "every amend unanswered", lambda a, c: a["amend_requests"] >= 1 and a["amends_confirmed"] == 0 and (a["amend_timeouts"] >= 1 or a["fills_during_amend_pending"] >= 1 or a["cancels_during_amend"] >= 1)),
                ("2i_v110_disconnect", "Bybit order entry and private stream down over the amend", lambda a, c: a["amend_timeouts"] >= 1 or a["amend_fallback_cancel"] >= 1 or a["amends_rejected"] >= 1),
                ("2i_v110_dup_filled", "every Filled report delivered twice", lambda a, c: a["amend_requests"] >= 1 and c["paths"].get("duplicate_updates_suppressed", 0) > 0))
    got, missing, queries = {}, [], {}
    for name, what, acted in variants:
        d = RUNS / f"scn_{name}"
        if not (d / "session_s0_audit.json").exists():
            missing.append(name)
            continue
        queries[name] = json.loads((d / "session_manifest.json").read_text()).get("ledger_event_counts", {}).get("VENUE_ORDER_QUERY", 0)
        got[name] = (what, acted, json.loads((d / "session_s0_audit.json").read_text()), json.loads((d / "session_ems_path_coverage.json").read_text()),
                     json.loads((d / "session_injection.json").read_text()) if (d / "session_injection.json").exists() else {})
    if not got:
        return ("OMS amend lineage + session controller on v1_10", "no 2i v1_10 run found", "NOT COVERED")
    bad = [n for n, (w, f, a, c, i) in got.items() if a["two_live_orders"] or a["duplicate_fills_booked_twice"] or a["lineage_overfill"] or a["lineage_overfill_alarms"] or not a["hedged_equals_filled"]]
    not_acted = [n for n, (w, f, a, c, i) in got.items() if not f(a, c)]
    no_query = [n for n in ("2i_v110_reject", "2i_v110_timeout", "2i_v110_disconnect") if n in got and not queries.get(n)]          # a reject or a timeout must end in a venue query, never a blind second order
    parts = []
    for n, (w, f, a, c, i) in got.items():
        parts.append(f"{n} ({w}): shifts {i.get('shifts_applied', 0)}, amends requested {a['amend_requests']} confirmed {a['amends_confirmed']} rejected {a['amends_rejected']} timed out {a['amend_timeouts']} fallback to cancel {a['amend_fallback_cancel']}, "
                     f"fills during AMEND_PENDING {a['fills_during_amend_pending']}, late fills on the old order {a['late_fills_reducing_replacement']}, cancels during a pending amend {a['cancels_during_amend']}, quotes withdrawn {a['quotes_withdrawn']}, "
                     f"venue queries after it {queries.get(n, 0)}, two live orders {a['two_live_orders']}, duplicate fills booked twice {a['duplicate_fills_booked_twice']}, maker filled {a['maker_filled']} = hedge filled {a['hedge_filled']}: {a['hedged_equals_filled']}")
    zero_paths = [k for k in ("fills_during_amend_pending", "late_fills_reducing_replacement", "cancels_during_amend") if not any(a[k] for (w, f, a, c, i) in got.values())]
    detail = ("INJECTED hedge-venue move on the real capture (2 ticks every 20 s for 8 s; entry threshold lowered to -2.0 bps and the validity tolerance widened to 2.5 bps FOR THE CASE, labelled in arb_overrides). " + " || ".join(parts)
              + (f". Not run: {missing}" if missing else "")
              + (f". PATHS NEVER MET ON THE REAL CAPTURE (NOT COVERED here; covered by tests/test_oms_amend.py and tests/test_s0_session.py only): {zero_paths}" if zero_paths else "")
              + (f". Variants whose fault did not meet a real amend: {not_acted}" if not_acted else "")
              + (f". NO VENUE QUERY recorded after the reject or timeout in {no_query} (a timeout must end in a query): NOT COVERED for that path" if no_query else ""))
    if bad:
        return ("OMS amend lineage + session controller on v1_10", detail + f". INVARIANT BROKEN in {bad}", "FAIL")
    if not_acted or missing or zero_paths or no_query:
        return ("OMS amend lineage + session controller on v1_10", detail, "NOT COVERED")
    return ("OMS amend lineage + session controller on v1_10", detail, "PASS")


unit = {
    "2g Restart with open orders, inside a replay": ("session, OMS, plan store", "NOT COVERED at replay level (owner 13 Nov: reason accepted): the replay driver holds about 30 references to one session object, and a faithful restart rebuilds the session from the plan store and re-subscribes the books. Covered instead at OMS level, against a scripted venue: see the next row", "NOT COVERED"),
    **({} if tot else {"2h New-level vs queued fills": ("simulator", "field built (maker_observations.jsonl maker_fill rows) but no run has been made with it yet", "NOT COVERED")}),
    "2i Amend paths (S0 profile v1_9s)": _case_2i(),
    "2i Amend paths on v1_10 (INJECTED hedge move on the real 29 Oct feed; reject, timeout, disconnect, duplicate Filled)": _case_2i_v110(),
}
ev10 = CASES / "case10_exchange_side_restart_reconciliation_missed_fill.jsonl"
if ev10.exists():
    e10 = rows(ev10)
    first_fill = next((i for i, e in enumerate(e10) if e["stage"] == "fill_booked"), None)
    nxt = next((i for i, e in enumerate(e10) if i > (first_fill or 0) and e["stage"] == "request_sent" and e.get("venue") == "BYBIT"), len(e10))
    seg = e10[first_fill:nxt] if first_fill is not None else []
    via_rest = [e for e in e10 if e["stage"] == "fill_notified" and e.get("source") == "REST_QUERY"]
    hedges = [e for e in seg if e["stage"] == "hedge_sent"]
    add("2g Restart: exchange-side reconciliation (open orders, executions, positions fetched from the venue; OMS level, scripted venue)", "OMS, plan store, restart gate", len(e10), len(via_rest) == 1 and len(hedges) == 1 and first_fill == 1,
        f"stage trace: the missed maker fill came from the venue's account (REST_QUERY) and was booked {len(via_rest)} time(s); hedges sent for it before any new quote {len(hedges)}; the new quote went out only after the reconciliation (a repeated account fetch books nothing). The unit cases also show an unknown venue order, a position that differs, a failed fetch and executions that do not add up each keeping new maker exposure held, and an order still resting at the venue reconciled without being finished. "
        f"Scripted venues only: the live adapters' account_snapshot and a testnet restart drill are not built yet", ev10.name)
ev11 = CASES / "case11_hedge_dust_carry.json"
if ev11.exists():
    e11 = json.loads(ev11.read_text())
    rep = e11["dust_report"]
    add("2o Hedge dust carry (a hedge chunk below the venue minimum is carried, never refused and left open; owner 16 Nov)", "OMS, EMS layer, session breaker", rep["carried_events"],
        e11["hedged_plus_carried_equals_filled"] and rep["alarms"] <= 1,
        f"size-25 day, v1_9 + labelled dust_carry override, hedge minimum 5 USDT: carried {rep['carried_events']} times, all lot remainders (the day has no chunk below 5 USDT at this size: the sub-minimum chunk is covered by the unit cases); max balance {rep['max_abs_base']} base, oldest age {rep['oldest_age_s']:.0f} s, balance at the end {rep['balance_end']}; one HEDGE_DUST_ALARM (age over {rep['alarm_after_s']:g} s, ASSUMED) that blocks nothing; hedged + carried = filled. "
        "Unit cases: tiny partial fill carried once, no order, no event per tick; next fill takes the dust in; opposite dust reduces; dust of two plans reaches the minimum and is sent at once; price alone lifts the balance (dust host plan); retry and unwind path; age alarm once and clear; restart keeps the balance; breaker ignores carried dust but trips on an uncarried lot. NOT VERIFIED: whether a reduce-only order is exempt from the minimum notional (testnet drill).", ev11.name)
for case, (layer, why, res) in unit.items():
    matrix.append({"case": case, "covering_layer": layer, "count": None, "result": res, "detail": why, "evidence": "see detail"})

(CASES / "coverage_matrix.json").write_text(json.dumps(matrix, indent=1))
lines = ["# EMS coverage matrix (v1_9, EXPLORATORY, mechanics only)", "", "| Path or case | Covering layer | Count | Result | Detail | Evidence |", "|---|---|---|---|---|---|"]
for m in matrix:
    lines.append(f"| {m['case']} | {m['covering_layer']} | {m['count']} | **{m['result']}** | {m['detail']} | {m['evidence']} |")
(CASES / "coverage_matrix.md").write_text("\n".join(lines) + "\n")
print("\n".join(lines))
