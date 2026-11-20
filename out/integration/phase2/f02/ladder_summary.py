"""Phase 2 F02 b (owner 20 Nov): the size-ladder table from the finished rungs (series i only, owner follow-up). Figures from new_backtest.session_replay.size_rung_metrics; the verdict rule:
FAILED when the rung needed a risk limit to act or could not stay inside one: a kill switch, a residual breach, an unwind, the dust bound reached, or a maker fill whose hedge left more than dust unhedged
(more than 0.06 SOL, about one Binance minimum order, short). Limits that BOUND without failing (participation, order rate, margin warning) are listed. EXPLORATORY: 29 Oct 2026 capture (REJECTED /
CRITICAL, mechanics only), v1_17, singapore latency (ASSUMED, pre-migration), pair close on, fees of the profile. Writes ladder_summary.json and LADDER.md."""
import json
import sys
from pathlib import Path

ROOT = Path("/Users/vaibhav/Documents/Newproject")
sys.path.insert(0, str(ROOT / "crypto_MFT_arb_platform/src"))
from new_backtest.session_replay import size_rung_metrics  # noqa: E402

HERE = Path(__file__).resolve().parent
RUNGS = [("i_01k", 1000, 10.2), ("i_05k", 5000, 51.0), ("i_10k", 10000, 102.0), ("i_20k", 20000, 204.1), ("i_30k", 30000, 306.1), ("i_50k", 50000, 510.2)]
DUST_SOL = 0.06


def verdict(m, pairs, plan_short):
    why = []
    ev = m["risk_events"]
    for k in ("KILL_SWITCH_ACTIVATED", "RESIDUAL_BREACH", "HEDGE_UNWIND_SENT", "UNWIND_SUBMIT", "DUST_BOUND_REACHED"):
        if ev.get(k):
            why.append(f"{k} {ev[k]}")
    if plan_short:
        why.append(f"{len(plan_short)} plans left more than {DUST_SOL} SOL unhedged (maker fills against hedge fills of the same plan, from session_fills)")
    return ("FAILED" if why else "PASS"), why


def plan_shortfalls(d):
    """Per plan (parent id): maker fills less hedge fills, from session_fills.jsonl. The pair-economics row's hedged_qty is not used: it misses a hedge that also took in carried dust
    (found on i_20k: a 35.01 SOL maker fill hedged by a 35.02 SOL sell 37 ms later read hedged_qty 0)."""
    net = {}
    for x in (d / "session_fills.jsonl").read_text().splitlines():
        if not x.strip():
            continue
        f = json.loads(x)
        role = (f.get("order") or {}).get("order_role")
        q = float(f["fill_quantity"])
        row = net.setdefault(f.get("parent_id"), [0.0, 0.0])
        if role == "PRIMARY":
            row[0] += q
        elif role in ("HEDGE", "UNWIND"):
            row[1] += q
    return {pid: m - h for pid, (m, h) in net.items() if abs(m - h) > DUST_SOL and not str(pid).startswith("dust")}


def main():
    out, lines = {}, []
    for name, parent, size in RUNGS:
        d = HERE / "ladder" / name
        if not (HERE / "ladder" / f"{name}.done").exists() or not (d / "session_fills.jsonl").exists():
            out[name] = {"parent_usdt": parent, "status": "NOT RUN / NOT FINISHED"}
            continue
        m = size_rung_metrics(d)
        pairs = [json.loads(x) for x in (d / "session_pair_economics.jsonl").read_text().splitlines() if x.strip()]
        short = plan_shortfalls(d)
        v, why = verdict(m, pairs, short)
        child = min(size, round(25000 / 98, 1))
        bound = {k: v_ for k, v_ in (m["stops"].get("session_limits_blocks") or {}).items()}
        out[name] = {"parent_usdt": parent, "parent_sol": size, "child_sol": child, "verdict": v, "failed_because": why, "plan_shortfalls_sol": short, "limits_that_bound": bound,
                     "participation_or_legging_bound": {k: m["ledger_event_type_counts"].get(k, 0) for k in ("LEGGING_LIMIT_BOUND", "PARTICIPATION_LIMIT_BOUND", "PACKAGE_DISPATCH_THROTTLED")},
                     "metrics": {k: v_ for k, v_ in m.items() if k != "ledger_event_type_counts"}}
    hedge_depth = {n: (r["metrics"]["completion"] | {"hedge_cost_bps": r["metrics"]["cost"]["hedge_cost_vs_decision_touch_bps"]}) for n, r in out.items() if "metrics" in r}
    exits = {n: r["metrics"]["exits"] for n, r in out.items() if "metrics" in r}
    summary = {"label": "EXPLORATORY: SOLUSDT size ladder, series i (child = parent, capped at the 25,000 USDT clip cap), 29 Oct 2026 capture (REJECTED / CRITICAL: mechanics only), v1_17, pair close on, "
                        "singapore latency (ASSUMED, pre-migration), the profile's fees; capital per venue = max(1000, 15 x IM(2 x parent)) at the ASSUMED tiers",
               "series_ii": "NOT RUN (owner follow-up 20 Nov: series i only)", "rungs": out, "hedge_depth_by_rung": hedge_depth, "exit_by_rung": exits}
    (HERE / "ladder_summary.json").write_text(json.dumps(summary, indent=1, default=str))
    lines = ["# SOLUSDT size ladder, series i (Phase 2 F02 b)", "", summary["label"], "",
             "| Rung | Parent SOL | Child SOL | Verdict | Maker fills (buy/sell) | Pair net bps p10 / p50 | Hedge cost bps p50 / p90 | Fully hedged | Unhedged s p50 / p99 | Bursts (longest) | Peak net SOL | Peak notional / IM per venue | Max orders 1 s | Closing signals | Bound / failed |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, r in out.items():
        if "metrics" not in r:
            lines.append(f"| {name} | | | {r['status']} | | | | | | | | | | | |")
            continue
        m = r["metrics"]
        c, comp = m["cost"], m["completion"]
        g = lambda q, k: "-" if not q else q[k]
        notional = ", ".join(f"{v}: {m['margin']['peak_notional_usdt'][v]:.0f} / {m['margin']['peak_initial_margin_usdt_at_assumed_tiers'][v]}" for v in m["margin"]["peak_notional_usdt"])
        lines.append(f"| {name} | {r['parent_sol']} | {r['child_sol']} | {r['verdict']} | {m['maker_fills']} ({m['maker_fills_by_direction'].get('buy', 0)}/{m['maker_fills_by_direction'].get('sell', 0)}) | "
                     f"{g(c['pair_net_bps'], 'p10')} / {g(c['pair_net_bps'], 'p50')} | {g(c['hedge_cost_vs_decision_touch_bps'], 'p50')} / {g(c['hedge_cost_vs_decision_touch_bps'], 'p90')} | "
                     f"{comp['fully_hedged_share']} | {g(comp['unhedged_interval_s'], 'p50')} / {g(comp['unhedged_interval_s'], 'p99')} | {m['burst_fills']['fills_within_window_of_previous']} ({m['burst_fills']['longest_burst']}) | "
                     f"{m['residual']['peak_net_base']} | {notional} | {m['throughput']['max_maker_orders_ending_in_1s']} | {m['exits']['closing_signals']} | "
                     f"{'; '.join(r['failed_because']) or (', '.join(f'{k} {v}' for k, v in r['limits_that_bound'].items()) or '-')} |")
    (HERE / "LADDER.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
