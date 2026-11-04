# exec'd by build_report.py (shares its helpers: esc, fmt, pct, ci, table, sec, p, jl, jtable, X, E, ex)
sens = jl(X / "sensitivity.json", {"days": {}})
cal = jl(X / "calibration_eval.json", {})

# ── step 8 ──────────────────────────────────────────────────────────────────
def sens_rows(tags, label=None):
    rows = []
    for day, dd in sens.get("days", {}).items():
        for t in tags:
            v = dd.get(t)
            if not v: continue
            mo = v["markout_maker_by_venue"]
            rows.append([day, t, v["signals"], v["maker_fills"], json.dumps(v["fill_causes"]), fmt(v["cash_pnl"], 4), fmt(v["equity_common_ts"], 4),
                         fmt(mo["bybit"]["5s"]["qty_weighted_bps"], 2), fmt(mo["binance"]["5s"]["qty_weighted_bps"], 2),
                         f"+{len(v['maker_fill_signals_added_vs_base'])}/−{len(v['maker_fill_signals_removed_vs_base'])}"])
    return rows
HEAD = ["day", "run", "signals", "maker fills", "fill causes", "cash P&L (EXPL.)", "equity @ common ts (EXPL.)", "maker markout 5 s Bybit (bps)", "Binance (bps)", "signals filled vs base (+added/−removed)"]
ex.append(sec("8. Step 8 — through-price prints and hidden liquidity", p("Opt-in rule: a print strictly through our price fills the order (cause tag <code>trade_through</code>). Two versions: <b>in full</b> (the rule as shipped, optimistic) and <b>volume-limited</b> (<code>through_print_volume_limited</code>: the fill cannot exceed the print's volume, one budget across orders). Hidden liquidity cannot be observed in L2; its only trace is a refill after a print (iceberg-like replenishment fixture) and the cause tags.")
    + table(HEAD, sens_rows(["base", "through", "through_vol"])), "step8"))

# ── step 9 ──────────────────────────────────────────────────────────────────
rec = None
f = X / "runs/perp_perp_maker_taker_v1/singapore_bybit_perp_mm_clean_s1_base/2022-01-05/maker_observations.jsonl"
if f.exists():
    for line in open(f):
        r = json.loads(line)
        if r["obs"] == "maker_fill": rec = r; break
ex.append(sec("9. Step 9 — fill provenance record", p("Every simulated maker fill carries: order and model ids (labels of this model, <code>id_kind = model_label_not_native</code>), the three clocks (order arrival, event match time, report time), the causal trade id or cause tag, the print budget offered and consumed, the queue estimate before the fill, the netting credit used, the source type, the uncertainty flags and the model/settings hash. Example (first maker fill of the base 2022-01-05 run):")
    + f"<pre>{esc(json.dumps(rec, indent=1, default=str)) if rec else 'n/a'}</pre>", "step9"))

# ── step 10 ─────────────────────────────────────────────────────────────────
mq = jl(X / "mutation_evidence_qv2.json", {})
mrows = [[k, "CAUGHT" if v.get("caught") else ("control" if k.startswith("_") else "MISSED"), len(v.get("failed_tests", [])), v.get("summary", "")] for k, v in mq.items()]
ex.append(sec("10. Step 10 — invariant tests and mutation checks", jtable("test_queue_v2_invariants") + jtable("test_public_book_invariant") + "<h3>Mutation checks (REQUIRED_* are the four the brief names)</h3>"
    + table(["mutation", "result", "failing tests", "summary"], mrows), "step10"))

# ── step 11 ─────────────────────────────────────────────────────────────────
def cal_section():
    if not cal.get("models"):
        return p("<span class='warn'>calibration evaluation not available yet</span>")
    b = p(f"<b>SPOT calibration</b> on the real Bybit spot PostOnly orders (rebuilt fe_* pipeline, main inclusion rule: fresh book, replayed touch equals the recorded snapshot, not an amendment). Calibration days 2026-04-19..22, held-out 04-23..25. "
          f"Universe replayed: {cal['orders_in_universe']} of the 2,396 included orders (coin-days up to 2.0 M events; the 10 heaviest coin-days are excluded for compute). "
          "Perp needs the mainnet probe set. Parameters (hazard θ, the sim-outcome → probability map used for Brier and log loss) were fitted on calibration days only; <b>only 3 held-out days</b>, so day-clustered intervals are very wide.")
    rows = []
    for t, v in cal["models"].items():
        c = v["canonical_heldout"]
        rows.append([t, v["n_heldout"], ci(c["hit_ci_day_clustered"]), ci(c["premature_ci_day_clustered"]), ci(c["late_ci_day_clustered"]), ci(c["unfilled_ci_day_clustered"]),
                     fmt(v["quantity_fraction"]["sim"], 3) + " (real " + fmt(v["quantity_fraction"]["real"], 3) + ")"])
    b += "<h3>Held-out: first-fill timing against the real first fill (±1 s = hit)</h3>" + table(["model", "orders", "hit", "premature", "late", "unfilled", "quantity fraction"], rows)
    rows = []
    for t, v in cal["models"].items():
        for H in ("1s", "5s", "15s", "60s"):
            fi, fu = v["by_horizon"][f"first_{H}"], v["by_horizon"][f"full_{H}"]
            rows.append([t, H, pct(fi["real"]), pct(fi["sim"]), fmt(fi["brier"], 4), fmt(fi["logloss"], 4), pct(fu["real"]), pct(fu["sim"]), fmt(fu["brier"], 4), fmt(fu["logloss"], 4)])
    b += "<h3>Held-out: first-fill and full-fill probability by horizon (real vs simulated), Brier and log loss of the calibrated map</h3>" + table(
        ["model", "horizon", "first: real", "first: sim", "Brier", "log loss", "full: real", "full: sim", "Brier", "log loss"], rows)
    rows = []
    for t, v in cal["models"].items():
        mk = v["conditional_markout_bps"]
        rows.append([t] + [fmt(mk[f"{w}_{h}s"]["mean_bps"], 2) + f" (n={mk[f'{w}_{h}s']['n']})" for w in ("real", "sim") for h in (1, 5, 30)])
    b += "<h3>Held-out: conditional markout of the first fill (bps, public tape, + favours the maker)</h3>" + table(["model", "real 1s", "real 5s", "real 30s", "sim 1s", "sim 5s", "sim 30s"], rows)
    rel = []
    for t, v in cal["models"].items():
        for gname, gv in v["reliability_first5s"].items():
            for g, x in gv.items():
                rel.append([t, gname, g, x["n"], pct(x["predicted"]), pct(x["observed"])])
    b += "<h3>Reliability (first fill within 5 s): predicted vs observed, by coin group / size / liquidity tier / day</h3>" + table(["model", "grouping", "group", "n", "predicted", "observed"], rel)
    sel = [[t, v["cal_n"], fmt(v["cal_mean_logloss"], 4), pct(v["cal_hit"])] for t, v in cal["calibration_selection"].items()]
    b += "<h3>Selection on calibration days only</h3>" + table(["model", "calibration orders", "mean log loss over 1/5/15/60 s", "calibration hit"], sel)
    b += p(f"Hazard θ selected on calibration days: <b>{esc(str(cal.get('hazard_theta_selected')))}</b>. Calibrated candidate: <b>{esc(str(cal.get('calibrated_candidate')))}</b> ({esc(cal.get('calibrated_candidate_rule', ''))}). Best cohort hypothesis: <b>{esc(str(cal.get('best_cohort')))}</b>. "
           "Rejected-order rate: not available (the replay has no PostOnly reject model and the order set contains accepted orders only). Strategy runs keep at least three scenarios: behind_first, proportional (or the calibrated candidate), ahead_first.")
    return b
ex.append(sec("11. Step 11 — calibration against real orders", cal_section(), "step11"))

# ── step 13 ─────────────────────────────────────────────────────────────────
tags_main = [t for t in ("base", "behind_first", "ahead_first") ] + sorted({t for dd in sens.get("days", {}).values() for t in dd if t.startswith("cal_") or t.startswith("cohort")})
band = []
for day, dd in sens.get("days", {}).items():
    ms = [t for t in tags_main if t in dd]
    if ms:
        cash = [dd[t]["cash_pnl"] for t in ms]; eq = [dd[t]["equity_common_ts"] for t in ms]; mf = [dd[t]["maker_fills"] for t in ms]
        band.append([day, ", ".join(ms), f"{min(mf)}–{max(mf)}", f"{min(cash):.4f} … {max(cash):.4f} (spread {max(cash)-min(cash):.4f})", f"{min(eq):.4f} … {max(eq):.4f} (spread {max(eq)-min(eq):.4f})"])
ex.append(sec("12. Step 13 — strategy runs across queue scenarios (clean preset only; no fault scenarios)", p("v1 (maker_taker, PASSIVE placement as in the profile), singapore latency, bybit_perp_mm fees, clean, size 1, 2022-01-05 and 2022-01-13. <b>EXPLORATORY.</b> No model is called conservative because it fills less: the last column lists which signals' maker fills each model adds or removes against the shipped v1 setting (<code>base</code> = proportional, scalar, v1 fill-evidence rules). "
    "Maker markouts use the maker fills and the 1 s snapshot mids of each venue (offline).")
    + table(HEAD, sens_rows(["base", "behind_first", "ahead_first"] + [t for t in sorted({t for dd in sens.get('days', {}).values() for t in dd}) if t.startswith("cal_") or t.startswith("cohort")]))
    + "<h3>Sensitivity band across the scenario models</h3>" + table(["day", "models", "maker fills", "cash P&L", "equity at the common timestamp"], band)
    + "<h3>Unknown-queue floor vs fitted</h3>" + table(HEAD, sens_rows([t for t in sorted({t for dd in sens.get('days', {}).values() for t in dd}) if t.endswith("_fit") or t == "base"]))
    + p("<b>The v1 profile never places an order outside the captured depth</b> (it quotes inside the spread), so the unknown-queue distribution is never drawn: floor and fitted runs are identical by construction, and the table shows it. The unknown queue matters for <code>legacy_v0</code> (floor 25 for every absent level), which these runs do not exercise."), "step13"))

ex.append(sec("13. Switches measured on the strategy (Steps 2 and 4)", p("<code>ledger_epoch</code> (a snapshot invalidates old credit), <code>book_tape_repair=False</code>, <code>event_order=capture</code> and <code>own_impact=overlay</code> each against the shipped v1 setting, on both days. Same columns as above.")
    + table(HEAD, sens_rows(["base", "ledger_epoch", "tape_off", "capture", "overlay"])), "switches"))

ex.append(sec("14. Step 12 — native-L3 validation lab (plan only)", p("No download was made. The plan (data, free dates, L3 rebuild, acceptance, where cancellations fall, scoring every model on the same data reduced to L2 + trades, what the result may be used for) is in <code>out/queue_v2/step12_coinbase_lab_plan.md</code>. It validates model structure, not Bybit parameters.")
    + "<pre>" + esc(open(X / "step12_coinbase_lab_plan.md").read()[:3800]) + "…</pre>", "step12"))

ident = jl(X / "identity_check.json", {})
rss = jl(X / "rss_peaks.json", {"peak_by_label": {}})["peak_by_label"]
rss_rows = [[k, f"{v} MB"] for k, v in sorted(rss.items(), key=lambda x: -x[1])]
ex.append(sec("15. Legacy behaviour and what was not done", p("<b>Identity</b> (every new option is off by default): " + esc(json.dumps(ident.get("summary", "pending"))))
    + p("<b>Full suite:</b> 1705 passed, 1 skipped, 7 xfailed, 0 failed (21 min 19 s; <code>PYTHONPATH=src:tests pytest tests --ignore=tests/platform_kit</code>) on the tree before the last additions. The 5 earlier failures (<code>data_manifest_doc</code> not initialised for tests that call <code>_build_result</code> directly) are fixed. After that, 12 tests were added (survival 7, manifest provenance 2, alternate scenarios 3) and the helper <code>setup()</code> in the ledger test file was renamed <code>_setup()</code>; <b>the full suite has not been re-run since</b> (deferred at the owner's request). Queue-v2 test files alone, after the additions: " + esc(str(jl(X / "junit_summary.json", {}).get("summary"))) + ".")
    + p("<b>Identity re-check</b> was run on the final snapshot (<code>snap_qv2f</code>, including the alternate-scenario and manifest changes): all three replays are byte-identical to the Phase 0 reference; for v1 the owner's later <code>opposite_cross_fills: true</code> edit is pinned off so the reference comparison is like for like.")
    + "<h3>Peak RSS per replay label (sampled every 30 s; the 'other' row is the pytest run)</h3>" + table(["replay label", "peak RSS"], rss_rows)
    + "<ul><li>Not done: VM sync (the brief says no VM; the standing rule says mirror every change — decision pending); no commit or push.</li>"
      "<li>Not done: the 328 included orders on the 10 heaviest coin-days were not replayed for compute; the calibration is spot, not perp; only 3 held-out days.</li>"
      "<li>Jan-2022 Binance trade tapes remain truncated/thin; the strategy runs carry the written override reason and the manifest hash.</li>"
      "<li>Not changed: the shipped v1 profile (confirm_delta does not gate a vanished level; consume_liquidity on). Both are measured here, neither is switched.</li></ul>"
    , "final"))


# ── Maker-only analysis mode + parity proof ─────────────────────────────────
par = jl(X / "parity_result.json", {})
par_rows = [[d, v["orders_normal"], v["orders_maker_only"], "yes" if v["orders_same_price_side_size"] else "NO", v["fills_normal"], v["fills_maker_only"], v["fills_identical_ts_qty_price_side_cause"],
             f'{v["place_queue_ahead_identical"]} of {v["orders_normal"]}'] for d, v in par.items() if isinstance(v, dict)]
ex.append(sec("16. Maker-only analysis mode (main runner) and parity proof", p("The Oct-2024 Bybit-only work runs through the <b>main runner</b>: <code>analysis_mode='maker_only'</code>, the same profile, the same PASSIVE tactic, latency path, queue/cancel models, PostOnly option and provenance record as a strategy replay; the strategy and the hedge are disabled, the Binance trade tape is not in the replay, and the run is labelled <code>gate_class=BYBIT_ONLY_WINDOW</code> (EXPLORATORY).")
    + p("<b>Quarantined:</b> the earlier standalone script <code>oct_bybit_maker_sim.py</code> re-implemented the order lifecycle, the queue/cancel model calls, the latency stand-in and the markout calculation around imported fill-engine classes. Its outputs (<code>oct_sim/</code>, <code>oct_bybit_only_summary.json</code>) are <b>not used anywhere in this report</b>.")
    + "<h3>Parity proof: maker-only path vs the normal v1 replay (same orders, same model)</h3>"
    + table(["day", "orders normal", "orders maker-only", "same price/side/size", "fills normal", "fills maker-only", "fills identical (ts, qty, price, side, cause tag)", "queue_ahead at placement identical"], par_rows)
    + p(f"<b>PASS = {par.get('PASS')}. Maker fills matched: {esc(str(par.get('total_fills_matched')))}.</b> The maker-only path was fed the orders the v1 replay actually placed on the Bybit book (same landing times) and compared fill by fill.")
    + p("<b>Profile note.</b> A first parity run showed 21 of 23 identical on 2022-01-13. Cause: the live <code>perp_perp_maker_taker_v1.yaml</code> gained <code>maker.opposite_cross_fills: true</code> (owner edit 2026-11-04 05:09, 'CALIBRATED — PROVISIONAL') after the normal replays' snapshot was frozen. With that one setting pinned the same way in both paths the result is exact. The engine is not the cause. All Oct-2024 runs pin <code>opposite_cross_fills: false</code> (same as the parity and calibration runs) and say so.")
    , "parity"))

# ── Oct-2024 data status (BYBIT_ONLY_WINDOW) ────────────────────────────────
import datetime as _dt
octc = jl(X / "oct_rebuild_causes.json", {})
octm = jl(X / "oct_maker_only_summary.json", {})
man = jl(X / "manifest.json", {})
bvt = jl(X / "oct_binance_book_events_vs_trades.json", {})
def _day_rows():
    rows, files = [], []
    for d in range(1, 16):
        day = f"2024-10-{d:02d}"
        d0 = _dt.datetime.fromisoformat(day).replace(tzinfo=_dt.timezone.utc).timestamp()
        e = {v: man["days"].get(f"{v}/SOLUSDT/{day}") for v in ("bybit", "binance")}
        if not all(e.values()): continue
        lo = max(e[v]["channels"][ch]["first_ts"] for v in e for ch in ("book_events", "trades")); hi = min(e[v]["channels"][ch]["last_ts"] for v in e for ch in ("book_events", "trades"))
        by = e["bybit"]["channels"]["book_events"]
        rows.append([day, f"{(hi - lo) / 3600:.2f}", "FULL" if (hi - lo) / 3600 >= 23.0 else "PARTIAL", f"{(by['last_ts'] - d0) / 3600:.2f}", "copy of trades" if e["binance"].get("book_events_is_trade_copy") else "ok",
                     "placeholder (all 50.0)" if e["binance"]["channels"]["book_snapshots"].get("placeholder") else "ok", f"{e['bybit']['channels']['trades']['rows']:,} / {e['binance']['channels']['trades']['rows']:,}"])
        for f in man["files"]:
            if f["date"] == day and f["symbol"] == "SOLUSDT" and f["venue_dir"] in ("bybit", "binance") and f["rows"] > 0:
                files.append([f["path"], f["bytes"], f["rows"], f["sha256"][:16]])
    return rows, files
drows, dfiles = _day_rows()
oc = octc.get("total", {})
cause_rows = [[c, v["snapshots"], f"{v['touch_price_miss_pct']:.2f}%", f"{v['touch_size_miss_pct']:.2f}%", ", ".join(f"{k} {x:.1f}%" for k, x in sorted(v["level_miss_pct"].items()))] for c, v in oc.items()]
mods = octm.get("models", {})
mrows = [[m, f"{e['orders']:,}", pct(e["fill_rate_any"]), f"[{pct(e['fill_rate_ci95_hour_block'][0])}, {pct(e['fill_rate_ci95_hour_block'][1])}]", pct(e["fill_rate_full"]),
          fmt(e["time_to_fill_s"]["p10"], 2), fmt(e["time_to_fill_s"]["p50"], 2), fmt(e["time_to_fill_s"]["p90"], 2),
          ", ".join(f"{k} {v}" for k, v in e["first_fill_cause"].items())] for m, e in mods.items()]
kindrows = [[m, *[f"{pct(e['by_kind'][k]['fill_rate'])} (n={e['by_kind'][k]['orders']})" for k in ("improve", "touch", "second")]] for m, e in mods.items()]
sp = octm.get("spread_across_models", {})
def _bs(t): return "–" if not t else f"{t['mean']:.2f} [{t['ci95'][0]:.2f}, {t['ci95'][1]:.2f}] n={t['n']}"
arows = []
for g_, v in octm.get("albers", {}).get("groups", {}).items():
    arows.append([g_, v["orders"], v["fills"], pct(v["fill_rate"]["mean"]) + f" [{pct(v['fill_rate']['ci95'][0])}, {pct(v['fill_rate']['ci95'][1])}]" if v["fill_rate"] else "–"]
                 + [_bs(v.get(f"markout_{h}s_bybit_mid_bps")) for h in (1, 5, 30)] + [_bs(v.get(f"markout_{h}s_binance_last_trade_PROXY_bps")) for h in (1, 5, 30)])
dr = octm.get("binance_drift_PROXY_bps_positive_adverse", {})
drift_rows = [[k, v["n"], fmt(v["p10"], 2), fmt(v["p50"], 2), fmt(v["p90"], 2), fmt(v["mean"], 2)] for k, v in dr.items()]
xc = octm.get("markout_cross_check_vs_engine_aggregate", {})
xcrows = [[d, h, fmt(v["offline_bps"], 3), fmt(v["engine_bps"], 3)] for d, hh in xc.items() for h, v in hh.items()]
order = jl(X / "oct2024_tardis_order.json", {})
ndup = sum(1 for v in bvt.values() if v["share_identical"] > 0.999999)
ex.append(sec("17. Oct-2024 data status — and the Bybit-only window work (BYBIT_ONLY_WINDOW, EXPLORATORY)",
    p(f"<b>Binance Oct-2024 has no usable book.</b> Its <code>book_events</code> are a copy of its <code>trades</code> (compared row by row, whole files: {ndup} of {len(bvt)} days checked are 100 % identical; a sell print is stored as a 'bid modify'), and its <code>book_snapshots</code> are placeholders (every level exactly 50.0). "
      "The Binance tape is a trade tape only; it is never used as a book. <b>No synthetic depth</b> was generated for any result: no strategy run, no hedge P&amp;L, no hedge-cap sweep and no parameter choice used Oct-2024. Bybit Oct-2024 has real books but only for the first 1.6–7.8 h of each day (trades run 24 h).")
    + "<h3>The 15 days (exact set for later briefs)</h3>" + table(["day", "two-venue overlap (h)", "FULL / PARTIAL (FULL ≥ 23 h)", "Bybit book hours", "Binance book_events", "Binance snapshots", "trades rows Bybit / Binance"], drows)
    + p("Total Bybit real-book window: <b>" + f"{sum(octc.get('window_hours', {}).values()):.1f} h</b> over {len(octc.get('days', []))} days. No day is FULL, so the 'first 5 FULL days' scoping could not be applied; Binance 2024-10-16..20 are empty folders.")
    + "<h3>Files that make up the 15 days (SOLUSDT, non-empty)</h3>" + table(["path under data/", "bytes", "rows", "sha256 (first 16)"], dfiles)
    + "<h3>(a) Rebuild acceptance, Bybit window — misses by cause (exact fixed-point rebuild, deltas only between 1 s snapshots)</h3>" + table(["cause", "snapshots", "touch price miss", "touch price+size miss", "level miss by side/depth"], cause_rows)
    + "<h3>(b) Cancel-model comparison on hypothetical Bybit maker orders (main runner, maker-only mode)</h3>"
    + p(f"{octm.get('orders', 0):,} hypothetical 1-lot orders on {len(octm.get('days', []))} day(s) of real Bybit books and prints: improve / touch / second, both sides, every 90 s, time-in-force from the v1 profile (15 s), identical for every model (order sets identical: {octm.get('order_set_identical_across_models')}). "
        "Orders join the BACK of the displayed queue. CIs: hour-block bootstrap. The cause column is the cause of each order's first fill; <code>delta_vanish</code> fills are depth removals at the order's price (v1 profile, fill_evidence=confirm_delta), so a large part of the fill rate is level-vanish evidence, not prints.")
    + table(["model", "orders", "fill rate (any)", "95% CI", "fill rate (full)", "time to fill p10 (s)", "p50", "p90", "first-fill cause"], mrows)
    + p(f"<b>Spread across models:</b> fill rate {pct(sp.get('fill_rate_min'))} – {pct(sp.get('fill_rate_max'))} ({fmt(sp.get('fill_rate_range_pp'), 1)} pp); the models disagree on whether an order fills for {pct(sp.get('share_orders_models_disagree'))} of orders; median time to fill ranges {fmt((sp.get('time_to_fill_p50_range_s') or [None, None])[0], 2)} – {fmt((sp.get('time_to_fill_p50_range_s') or [None, None])[1], 2)} s.")
    + table(["model", "improve", "touch", "second"], kindrows)
    + "<h3>(c) Albers check (proportional model, each order's first fill)</h3>"
    + p("Markout in bps of the fill price, + = price moved in the maker's favour, at +1 / +5 / +30 s against the Bybit mid (OrderBook imported from the engine, fed with the same events) and against the <b>Binance last trade (PROXY)</b>; mean [95% hour-block bootstrap CI] n. "
        "<b>Relative queue position is degenerate here:</b> orders join the back, so ~100 % have position &gt; 0.75 and the front group is (almost) empty; absolute size-ahead terciles are shown instead. Near × opposite = median split of touch sizes at the decision.")
    + table(["group", "orders", "fills", "fill rate", "Bybit mid +1 s", "+5 s", "+30 s", "Binance PROXY +1 s", "+5 s", "+30 s"], arows)
    + p("<b>Cross-check of the offline markout join against the engine's own aggregate</b> (qty-weighted, Bybit mid; small differences are expected: the engine resolves at the first touch change after the horizon, the join at the exact horizon time):") + table(["day", "horizon", "offline join (bps)", "engine MarkoutTracker (bps)"], xcrows)
    + "<h3>(d) How far Binance moves after a Bybit fill (PROXY: last trade price, not the touch)</h3>"
    + p("Positive = adverse to the hedge (price fell after a buy fill, rose after a sell fill). Median 0 means no Binance print changed the price in that window.") + table(["horizon after the fill", "fills", "p10 (bps)", "p50", "p90", "mean"], drift_rows)
    + "<h3>Data request prepared (not sent, nothing downloaded)</h3><pre>" + esc(json.dumps(order, indent=1)) + "</pre>", "oct"))


# ── guide conformance, survival, association robustness, provenance ─────────
csv_ = jl(X / "calibration_survival.json", {})
mm = jl(X / "manifest.json", {})
nfiles = len(mm.get("files", [])); nunrec = sum(1 for f in mm.get("files", []) if f.get("provenance_unrecorded"))
conf_rows = [
 ["1 data contract and manifest", "built", f"per-file sha256, units, gaps, snapshot counts, id availability, truncation gate; capture_region and client_version fields added (see section 21): {nunrec} of {nfiles} files have them UNRECORDED"],
 ["2 reported L2 book first", "built", "fixed-point rebuild, atomic snapshots, absolute sizes, capture event order switch, per-day acceptance, own fills never mutate the public book"],
 ["3 venue truth vs strategy knowledge", "built", "two timelines, PostOnly check at arrival, print-before-arrival fixtures"],
 ["4 depletion ledger", "built; alternates now kept", "both arrival orders and the double-depletion case; ambiguous associations are flagged AND the alternate reading is stored with the fill (<code>obs.alternates</code>, <code>association_robust</code>); section 20 shows the bounds"],
 ["5 cancellation location", "built", "behind_first, proportional, ahead_first, power(n), min_baseline as the named all_ahead alias; C is the net residual"],
 ["6 L3-style queue representation", "built", "aggregate cohorts with model labels, multiple own orders, shadow overlay, through-price and volume-limited comparison"],
 ["7 true L3 from native data", "NOT built (by design)", "needs native order-id events and an order-level initial state; Tardis documents that for Coinbase only. Plan only: step12_coinbase_lab_plan.md"],
 ["8 calibration", "built; IPCW/RMST added", "section 19: the real order set contains almost only orders that FILLED, so censoring is ~absent and IPCW is not identifiable there; RMST and support diagnostics are reported; spot, not perp"],
 ["9 modules and invariant tests", "built", "11 of 11 new and 17 of 17 earlier mutations caught"],
 ["10 delivery", "built", "manifest, rebuild comparison, fixtures, provenance record, calibration report, strategy sensitivity; wording: calibrated L2 queue model"]]
ex.append(sec("18. Conformance to the guide (L3 style queue reconstruction, 02 Nov 2026)", p("The guide itself says: use the L2-plus-trades route for the Binance/Bybit perpetual workflow, and state <b>calibrated L2 queue model</b> or <i>observed native L3 reconstruction</i> accurately — they are not interchangeable. Section 7 (native L3) needs order-id data the venues do not provide in these files.")
    + table(["guide section", "status", "what exists / what is still open"], conf_rows), "conf"))

def _f(x, d=2): return "–" if x is None or (isinstance(x, float) and x != x) else f"{x:.{d}f}"
rd = csv_.get("real_orders_heldout", {})
rdh = rd.get("diagnostics", {}).get("horizons", {})
real_rows = [[h, _f(v["naive"], 3), _f(v["km_cdf"], 3), _f(v["cdf"], 3), v["n_censored_before_H"], v["at_risk_at_H"], _f(v["min_G"], 3), _f(v["rmst"], 2), "yes" if v["identifiable"] else "NO"] for h, v in rdh.items()]
rm_rows = []
for m_, v in csv_.get("models", {}).items():
    row = [m_, f"{v['sim_censored_share']*100:.1f}%"]
    for h in ("1s", "5s", "15s", "60s"):
        e = v["by_horizon"][h]
        row.append(f"{e['rmst_sim']:.2f} vs {e['rmst_real']:.2f} ({e['rmst_diff_sim_minus_real']:+.2f} [{e['rmst_diff_ci_day_clustered'][0]:+.2f}, {e['rmst_diff_ci_day_clustered'][1]:+.2f}])")
    rm_rows.append(row)
osv = octm.get("survival", {}).get("models", {})
osv_rows = []
for m_, d in osv.items():
    h = d["horizons"]
    ci_ = d["rmst_ci95_hour_block"]
    osv_rows.append([m_, f"{h['1s']['km_cdf']:.3f}", f"{h['5s']['km_cdf']:.3f}", f"{h['15s']['km_cdf']:.3f}", f"{_f(h['5s']['rmst'])} [{_f(ci_['5s']['ci95'][0])}, {_f(ci_['5s']['ci95'][1])}]",
                     f"{_f(h['15s']['rmst'])} [{_f(ci_['15s']['ci95'][0])}, {_f(ci_['15s']['ci95'][1])}]", "yes" if h["15s"]["identifiable"] else "NO", "NO (nobody at risk after the TTL)" if not h["30s"]["identifiable"] else "yes"])
ex.append(sec("19. Time to fill under censoring — KM, IPCW and RMST (guide s8)", p("Definitions and tests: <code>new_backtest/queue_v2/survival.py</code> (7 tests: no-censoring identity, hand-computed KM, recovery of a known exponential law where the naive share is biased, stratified censoring, tail not identified, weight diagnostics). "
      "RMST to H is E[min(T, H)]: a horizon-limited model cannot infer the tail beyond what was observed. The probability of a fill under the ACTUAL cancel policy is not the survival of a quote left alive.")
    + "<h3>(a) The real Bybit spot orders (held-out days): what the data can and cannot support</h3>"
    + p(f"n = {rd.get('n', 0):,}; status counts {esc(json.dumps(rd.get('status_counts', {})))}. <b>{esc(rd.get('support_finding', ''))}</b>")
    + table(["horizon", "naive P(fill by H)", "KM", "IPCW", "censored before H", "at risk at H", "min G", "RMST (s)", "IPCW identifiable"], real_rows)
    + "<h3>(b) RMST, simulated vs real, per queue model (held-out days; simulated vs real (difference [day-clustered 95% CI]); an unfilled simulated order counts as H)</h3>"
    + table(["model", "sim orders unfilled", "RMST 1 s", "5 s", "15 s", "60 s"], rm_rows)
    + p("Read: positive = the simulated order waits longer than the real one. At 5 s the proportional and power(0.5) models are within about 0.1 s of the real RMST; ahead_first, the cohort age hypotheses and min_baseline sit further away in either direction. This is conditional on the real order having filled.")
    + "<h3>(c) Oct-2024 hypothetical orders (BYBIT_ONLY_WINDOW, EXPLORATORY): administrative censoring at the 15 s time-in-force</h3>"
    + p("Every unfilled order is cancelled at the TTL, independent of the market, so KM, IPCW and the naive share agree up to 15 s. Beyond the TTL nobody is at risk: the fill probability at 30 s or 60 s for a quote left alive is <b>not identified</b> by these runs. RMST CI: hour-block bootstrap.")
    + table(["model", "KM P(fill ≤ 1 s)", "≤ 5 s", "≤ 15 s", "RMST 5 s (s)", "RMST 15 s (s)", "identifiable at 15 s", "identifiable at 30 s / 60 s"], osv_rows), "surv"))

ab = octm.get("association_bounds", {}).get("models", {})
ab_rows = [[m_, pct(b["fill_rate_as_simulated"]), pct(b["fill_rate_first_fill_association_robust"]), pct(b["fill_rate_if_every_ambiguous_fill_were_a_cancellation"]), pct(b["share_of_filled_orders_whose_first_fill_is_ambiguous"])] for m_, b in ab.items()]
ex.append(sec("20. Association robustness — alternate scenarios kept with the fill (guide s4)", p("A fill is <b>ambiguous</b> when volume is attributed to our order without one causal print: a depth decrease or snapshot shrink, or an ambiguous ledger association (flag <code>ambiguous_trade_depth</code>). From now on every such fill also stores the alternate reading in its provenance record: <code>alternates</code> (e.g. 'the depletion was a cancellation, own fill = 0'; the competing pending credits when several could explain a decrease) and <code>association_robust</code>. "
      "The Oct-2024 runs above were made before that field existed, so their bounds are computed from the flags: the lower bound treats every ambiguous fill as a cancellation.")
    + table(["model", "fill rate as simulated", "first fill association-robust", "if every ambiguous fill were a cancellation", "share of filled orders whose first fill is ambiguous"], ab_rows)
    + p("Read: most simulated fills under the v1 rules are depth-decrease evidence, so the plausible fill rate range is wide (for the proportional model roughly 36% to 80%). A strategy result that depends on the high end should be reported with this range, not with one number."), "assoc"))

ex.append(sec("21. Manifest provenance fields (guide s1)", p(f"Every manifest file entry now carries <code>capture_region</code>, <code>client_version</code> and <code>provenance_unrecorded</code>. They are DECLARED per dataset and mostly <b>UNKNOWN</b>: the files do not say, and Tardis-shaped files do not carry them. <b>{nunrec} of {nfiles} files have unrecorded provenance.</b> The run gate lists the affected venue-days in each run's data-manifest record (<code>provenance_unrecorded_venue_days</code>) and never blocks on it. The Oct-2024 data request asks the provider to state them on delivery.")
    + p("Because the entries changed, the manifest hash changed: the pre-change manifest is kept as <code>manifest_v1_before_provenance.json</code> (hashes and counts identical, no rescan); results produced earlier in this report reference the old manifest hash."), "prov"))
ex.append(sec("22. Final statement", '<p class="warn"><b>Final statement.</b> This is a <b>calibrated L2 queue model</b> with explicit, labelled assumptions (unknown-age initial cohort, age hypotheses, spot-calibrated parameters). It is not an L3 reconstruction, and every P&amp;L above is EXPLORATORY. The Oct-2024 results are EXPLORATORY, BYBIT_ONLY_WINDOW: Bybit book hours only, Binance as a trade-tape PROXY, no hedge P&amp;L, no strategy runs, no synthetic depth.</p>', "final2"))
