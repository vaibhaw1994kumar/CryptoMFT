"""Batch runner: every pair, two passes, aggregated.

Two-pass structure mirrors altcoin_mm_v1/batch_backtest.py:

    pass 1   EmpiricalFillModel starts cold, learns from the run's own maker
             order outcomes, writes fill_events.jsonl
    pass 2   same replay warm-started from pass 1's log, so the posterior enters
             the run already conditioned on this pair's fill behaviour

Pass 2 is the number to quote. Pass 1 spends its early history quoting off
priors rather than observed behaviour, which biases maker-mode results.

In TAKER_TAKER mode the fill model is not consulted (no passive orders), so the
second pass is redundant and is skipped unless --force-two-pass is given.

Pairs are skipped, loudly, when both venues are not present for the window:
a one-venue "run" silently produces zero signals and would otherwise look like a
strategy result rather than missing data.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

from .arb_strategy import MAKER_TAKER, TAKER_TAKER, ArbConfig
from .backtester import ArbBacktestConfig, ArbBacktestResult, CrossVenueArbBacktester
from .config import (
    ALL_PAIRS, BINANCE, BYBIT, DEFAULT_END_DATE, DEFAULT_LATENCY_PRESET,
    DEFAULT_START_DATE, LATENCY_PRESETS, max_position_for,
)
from .cross_venue_feed import check_coverage


@dataclass
class PairOutcome:
    pair: str
    status: str                      # ok | skipped_no_data | error
    detail: str = ""
    pass1: dict | None = None
    pass2: dict | None = None

    @property
    def final(self) -> dict | None:
        return self.pass2 or self.pass1


@dataclass
class BatchSummary:
    mode: str
    latency_preset: str
    start_date: str
    end_date: str
    pairs_requested: int
    pairs_run: int
    pairs_skipped: int

    total_realized_pnl: float
    total_gross_pnl: float
    total_fees: float
    total_unhedged_loss: float
    total_traded_notional: float
    total_signals: int
    total_orders_sent: int
    total_orders_filled: int
    total_orders_missed: int

    fill_ratio: float
    mean_edge_capture: float
    mean_realized_net_edge_bps: float
    pnl_per_million_notional: float
    profitable_pairs: int

    per_pair: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)


def _run_once(cfg: ArbBacktestConfig) -> ArbBacktestResult:
    return CrossVenueArbBacktester(cfg).run()


def _date_to_epoch(date_str: str) -> float:
    """Return UTC midnight epoch seconds for a YYYY-MM-DD string."""
    return _dt.datetime.strptime(date_str, "%Y-%m-%d").replace(
        tzinfo=_dt.timezone.utc
    ).timestamp()


def _days_in_window(common_days: list[str], start_ts: float, end_ts: float) -> list[str]:
    """Days from common_days whose UTC midnight epoch falls in [start_ts, end_ts)."""
    return [d for d in common_days if start_ts <= _date_to_epoch(d) < end_ts]


def _aggregate_fold_results(fold_dicts: list[dict]) -> dict:
    """Combine per-fold ArbBacktestResult dicts.

    Additive scalars are summed; ratio metrics are weighted by signals_completed.
    """
    if not fold_dicts:
        return {}

    _ADD = (
        "realized_pnl", "gross_pnl", "fees_paid", "unhedged_loss",
        "traded_notional", "signals_generated", "orders_sent",
        "orders_filled", "orders_missed", "orders_cancelled",
        "signals_completed", "signals_unhedged",
    )
    combined: dict = {k: sum(d.get(k, 0) for d in fold_dicts) for k in _ADD}

    tot_sig = combined.get("signals_completed", 0)
    for metric in ("edge_capture_ratio", "mean_realized_net_edge_bps"):
        combined[metric] = (
            sum(d.get(metric, 0.0) * d.get("signals_completed", 0) for d in fold_dicts)
            / tot_sig
        ) if tot_sig > 0 else 0.0

    last = fold_dicts[-1]
    combined["fill_log_path"] = last.get("fill_log_path")
    combined["start_date"] = fold_dicts[0].get("start_date", "")
    combined["end_date"] = last.get("end_date", "")

    merged_report: dict = {}
    for d in fold_dicts:
        for k, v in d.get("strategy_report", {}).items():
            if isinstance(v, (int, float)):
                merged_report[k] = merged_report.get(k, 0) + v
    combined["strategy_report"] = merged_report
    combined["wf_fold_count"] = len(fold_dicts)
    return combined


def run_pair(
    pair: str,
    base: ArbBacktestConfig,
    output_root: Path,
    two_pass: bool,
    calibration_days: int = 0,
    wf_window_days: int = 0,
    wf_step_days: int = 7,
) -> PairOutcome:
    coverage = check_coverage(
        base.data_root, pair, base.venues, base.start_date, base.end_date,
    )
    common = coverage.get("_intersection", [])
    if not common:
        have = {v: len(coverage.get(v, [])) for v in base.venues}
        return PairOutcome(
            pair, "skipped_no_data",
            detail=f"no day with both venues present (days per venue: {have})",
        )

    pair_dir = output_root / pair
    try:
        # Walk-forward: rolling day-aligned cal/eval folds.
        # 1. Cold pass (agent disabled) → fill_events.jsonl for all common days.
        # 2. Load all label records once; slice per fold using calendar indices.
        # 3. Per fold: FillModelV2.fit(cal_records) → eval backtester run.
        # 4. Aggregate eval fold metrics.
        # Fold boundaries are calendar-day-aligned (common[i:i+w] / common[i+w:i+w+s])
        # so every eval day is exactly what it says on the tin, regardless of when
        # the first fill event in the cold log happens to fall.
        if wf_window_days > 0 and len(common) > wf_step_days:
            from .background_agent_model import AgentFlowConfig  # noqa: PLC0415
            from .fill_label import extract_labels               # noqa: PLC0415
            from .fill_model_v1 import FillModelV2               # noqa: PLC0415

            cfg_cold = _clone(base, pair=pair, start=common[0], end=common[-1],
                              output_dir=str(pair_dir / "wf_cold"),
                              warm_start=None,
                              agent_flow_override=AgentFlowConfig(enabled=False))
            r_cold = _run_once(cfg_cold)

            if not r_cold.fill_log_path:
                return PairOutcome(pair, "error",
                                   detail="wf cold pass produced no fill_log_path")

            # Load all records once; filter per fold in memory
            all_records = extract_labels(r_cold.fill_log_path)
            horizon_s = base.arb.fill_gate_horizon_s
            fold_results: list[dict] = []
            fold_idx = 0
            for i in range(0, len(common) - wf_window_days, wf_step_days):
                cal_days = common[i:i + wf_window_days]
                eval_days = common[i + wf_window_days:i + wf_window_days + wf_step_days]
                if not cal_days or not eval_days:
                    break

                cal_start_ts = _date_to_epoch(cal_days[0])
                until_ts = _date_to_epoch(eval_days[0])

                cal_records = [r for r in all_records
                               if cal_start_ts <= r.timestamp < until_ts
                               and r.timestamp + r.horizon_s <= until_ts]
                if len(cal_records) < 50:
                    fold_idx += 1
                    continue

                model = FillModelV2.fit(
                    cal_records, gate_horizon_s=horizon_s,
                    trained_until_ts=until_ts,
                )
                if model is None:
                    fold_idx += 1
                    continue

                fold_dir = pair_dir / f"wf_fold_{fold_idx:03d}"
                model_path = str(fold_dir / "fill_model_v1.json")
                model.save_json(model_path)

                cfg_eval = _clone(
                    base, pair=pair, start=eval_days[0], end=eval_days[-1],
                    output_dir=str(fold_dir),
                    warm_start=r_cold.fill_log_path,
                    warm_start_until_ts=until_ts,
                    fill_model_v1_path=model_path,
                )
                fold_results.append(asdict(_run_once(cfg_eval)))
                fold_idx += 1

            if not fold_results:
                return PairOutcome(
                    pair, "error",
                    detail=(f"wf: no folds fitted "
                            f"(window={wf_window_days}d step={wf_step_days}d "
                            f"days={len(common)})"),
                )
            combined = _aggregate_fold_results(fold_results)
            n = combined["wf_fold_count"]
            return PairOutcome(
                pair, "ok",
                detail=(f"wf {n} folds window={wf_window_days}d "
                        f"step={wf_step_days}d {common[0]}..{common[-1]}"),
                pass1=asdict(r_cold), pass2=combined,
            )

        # Static single cal/eval split: run a cold calibration pass on the first
        # calibration_days, then evaluate on the remaining days warm-started
        # from those labels.  until_ts prevents label leakage: events whose
        # horizon crosses into the eval window are dropped from the warm-start.
        if calibration_days > 0 and len(common) > calibration_days:
            cal_days = common[:calibration_days]
            eval_days = common[calibration_days:]
            until_ts = _date_to_epoch(eval_days[0])

            cfg_cal = _clone(base, pair=pair, start=cal_days[0], end=cal_days[-1],
                             output_dir=str(pair_dir / "calibration"),
                             warm_start=None, warm_start_until_ts=None)
            r_cal = _run_once(cfg_cal)

            # Fit Phase-1 FillModelV1 from the calibration log, leakage-guarded
            # by until_ts.  One model per decision horizon; the eval run uses
            # whichever horizon matches ArbConfig.fill_gate_horizon_s.
            fill_model_v1_path: str | None = None
            if (base.arb.mode == "maker_taker"
                    and base.arb.min_fill_prob > 0.0
                    and r_cal.fill_log_path):
                from .fill_label import extract_labels   # noqa: PLC0415
                from .fill_model_v1 import FillModelV2   # noqa: PLC0415
                records = extract_labels(r_cal.fill_log_path, until_ts=until_ts)
                if records:
                    horizon_s = base.arb.fill_gate_horizon_s
                    model = FillModelV2.fit(records, gate_horizon_s=horizon_s,
                                           trained_until_ts=until_ts)
                    if model is not None:
                        model_path = str(pair_dir / "calibration" / "fill_model_v1.json")
                        model.save_json(model_path)
                        fill_model_v1_path = model_path

            cfg_eval = _clone(base, pair=pair, start=eval_days[0], end=eval_days[-1],
                              output_dir=str(pair_dir / "eval"),
                              warm_start=r_cal.fill_log_path,
                              warm_start_until_ts=until_ts,
                              fill_model_v1_path=fill_model_v1_path)
            r_eval = _run_once(cfg_eval)
            return PairOutcome(
                pair, "ok",
                detail=(f"cal={cal_days[0]}..{cal_days[-1]} "
                        f"eval={eval_days[0]}..{eval_days[-1]}"),
                pass1=asdict(r_cal), pass2=asdict(r_eval),
            )

        # Clamp to the days both venues actually cover, so the window reported
        # is the window tested.
        cfg1 = _clone(base, pair=pair, start=common[0], end=common[-1],
                      output_dir=str(pair_dir / "pass1"), warm_start=None)
        r1 = _run_once(cfg1)

        if not two_pass:
            return PairOutcome(pair, "ok", detail=f"{common[0]}..{common[-1]}",
                               pass1=asdict(r1))

        cfg2 = _clone(base, pair=pair, start=common[0], end=common[-1],
                      output_dir=str(pair_dir / "pass2"),
                      warm_start=r1.fill_log_path)
        r2 = _run_once(cfg2)
        return PairOutcome(pair, "ok", detail=f"{common[0]}..{common[-1]}",
                           pass1=asdict(r1), pass2=asdict(r2))
    except Exception as exc:  # one bad pair must not abort a 30-pair batch
        return PairOutcome(pair, "error", detail=f"{type(exc).__name__}: {exc}")


def _clone(
    base: ArbBacktestConfig, pair: str, start: str, end: str,
    output_dir: str, warm_start: str | None,
    warm_start_until_ts: float | None = None,
    fill_model_v1_path: str | None = None,
    agent_flow_override=None,
) -> ArbBacktestConfig:
    arb = ArbConfig(**{**vars(base.arb), "max_position_base": max_position_for(pair)})
    return ArbBacktestConfig(
        pair=pair, data_root=base.data_root, venues=base.venues,
        start_date=start, end_date=end, max_events=base.max_events,
        latency_preset=base.latency_preset,
        bybit_fee_preset=base.bybit_fee_preset,
        binance_fee_preset=base.binance_fee_preset,
        arb=arb, maker=base.maker, venue_state=base.venue_state,
        consume_liquidity=base.consume_liquidity,
        include_deltas=base.include_deltas, seed=base.seed,
        allow_rejected_data=base.allow_rejected_data,
        output_dir=output_dir, warm_start_fill_events=warm_start,
        warm_start_until_ts=warm_start_until_ts,
        fill_model_v1_path=fill_model_v1_path,
        markout_horizons_s=base.markout_horizons_s,
        progress_every=base.progress_every,
        agent_flow=agent_flow_override if agent_flow_override is not None else base.agent_flow,
    )


def run_batch(
    pairs: Iterable[str],
    base: ArbBacktestConfig,
    output_dir: str,
    two_pass: bool = True,
    verbose: bool = True,
    calibration_days: int = 0,
    wf_window_days: int = 0,
    wf_step_days: int = 7,
) -> BatchSummary:
    out_root = Path(output_dir)
    out_root.mkdir(parents=True, exist_ok=True)
    pairs = list(pairs)

    outcomes: list[PairOutcome] = []
    for i, pair in enumerate(pairs, 1):
        if verbose:
            print(f"\n[{i}/{len(pairs)}] {pair}", flush=True)
        outcome = run_pair(pair, base, out_root, two_pass,
                           calibration_days=calibration_days,
                           wf_window_days=wf_window_days,
                           wf_step_days=wf_step_days)
        outcomes.append(outcome)
        if verbose:
            if outcome.status != "ok":
                print(f"  {outcome.status}: {outcome.detail}", flush=True)
            else:
                r = outcome.final or {}
                print(
                    f"  {outcome.detail}  pnl={r.get('realized_pnl', 0.0):,.2f}  "
                    f"signals={r.get('signals_generated', 0):,}  "
                    f"filled={r.get('orders_filled', 0):,}/"
                    f"{r.get('orders_sent', 0):,}  "
                    f"capture={r.get('edge_capture_ratio', 0.0):.2f}",
                    flush=True,
                )

    summary = _aggregate(outcomes, base)
    (out_root / "batch_summary.json").write_text(
        json.dumps(asdict(summary), indent=2, sort_keys=True, default=str), encoding="utf-8",
    )
    if verbose:
        _print_summary(summary)
    return summary


def _aggregate(outcomes: list[PairOutcome], base: ArbBacktestConfig) -> BatchSummary:
    ok = [o for o in outcomes if o.status == "ok" and o.final]
    skipped = [o for o in outcomes if o.status != "ok"]

    tot_pnl = tot_gross = tot_fees = tot_unhedged = tot_notional = 0.0
    tot_signals = tot_sent = tot_filled = tot_missed = 0
    captures: list[float] = []
    edges: list[float] = []
    per_pair: list[dict] = []

    for o in ok:
        r = o.final or {}
        tot_pnl += r.get("realized_pnl", 0.0)
        tot_gross += r.get("gross_pnl", 0.0)
        tot_fees += r.get("fees_paid", 0.0)
        tot_unhedged += r.get("unhedged_loss", 0.0)
        tot_notional += r.get("traded_notional", 0.0)
        tot_signals += int(r.get("signals_generated", 0))
        tot_sent += int(r.get("orders_sent", 0))
        tot_filled += int(r.get("orders_filled", 0))
        tot_missed += int(r.get("orders_missed", 0))
        if r.get("signals_completed", 0):
            captures.append(r.get("edge_capture_ratio", 0.0))
            edges.append(r.get("mean_realized_net_edge_bps", 0.0))
        per_pair.append({
            "pair": o.pair,
            "window": o.detail,
            "realized_pnl": r.get("realized_pnl", 0.0),
            "gross_pnl": r.get("gross_pnl", 0.0),
            "fees": r.get("fees_paid", 0.0),
            "unhedged_loss": r.get("unhedged_loss", 0.0),
            "traded_notional": r.get("traded_notional", 0.0),
            "signals": r.get("signals_generated", 0),
            "orders_sent": r.get("orders_sent", 0),
            "orders_filled": r.get("orders_filled", 0),
            "orders_missed": r.get("orders_missed", 0),
            "edge_capture_ratio": r.get("edge_capture_ratio", 0.0),
            "mean_realized_net_edge_bps": r.get("mean_realized_net_edge_bps", 0.0),
            "signals_unhedged": r.get("signals_unhedged", 0),
            "top_reject": _top_reject(r.get("strategy_report", {})),
        })

    per_pair.sort(key=lambda d: d["realized_pnl"], reverse=True)

    return BatchSummary(
        mode=base.arb.mode, latency_preset=base.latency_preset,
        start_date=base.start_date, end_date=base.end_date,
        pairs_requested=len(outcomes), pairs_run=len(ok), pairs_skipped=len(skipped),
        total_realized_pnl=tot_pnl, total_gross_pnl=tot_gross,
        total_fees=tot_fees, total_unhedged_loss=tot_unhedged,
        total_traded_notional=tot_notional,
        total_signals=tot_signals, total_orders_sent=tot_sent,
        total_orders_filled=tot_filled, total_orders_missed=tot_missed,
        fill_ratio=tot_filled / max(1, tot_sent),
        mean_edge_capture=sum(captures) / len(captures) if captures else 0.0,
        mean_realized_net_edge_bps=sum(edges) / len(edges) if edges else 0.0,
        pnl_per_million_notional=tot_pnl / (tot_notional / 1e6) if tot_notional > 0 else 0.0,
        profitable_pairs=sum(1 for d in per_pair if d["realized_pnl"] > 0),
        per_pair=per_pair,
        skipped=[{"pair": o.pair, "status": o.status, "detail": o.detail} for o in skipped],
    )


def _top_reject(report: dict) -> str:
    rejects = {k[7:]: v for k, v in report.items() if k.startswith("reject_")}
    if not rejects:
        return ""
    name, count = max(rejects.items(), key=lambda kv: kv[1])
    return f"{name}={int(count):,}"


def _print_summary(s: BatchSummary) -> None:
    print("\n" + "=" * 78)
    print(f"BATCH SUMMARY  mode={s.mode}  latency={s.latency_preset}  "
          f"{s.start_date}..{s.end_date}")
    print("=" * 78)
    print(f"  pairs run {s.pairs_run}/{s.pairs_requested}   skipped {s.pairs_skipped}")
    print(f"  realized PnL        {s.total_realized_pnl:>18,.2f}")
    print(f"    gross edge        {s.total_gross_pnl:>18,.2f}")
    print(f"    fees              {-s.total_fees:>18,.2f}")
    print(f"    leg risk          {-s.total_unhedged_loss:>18,.2f}")
    print(f"  traded notional     {s.total_traded_notional:>18,.2f}")
    print(f"  PnL per $1M traded  {s.pnl_per_million_notional:>18,.2f}")
    print(f"  signals {s.total_signals:,}   orders {s.total_orders_filled:,}"
          f"/{s.total_orders_sent:,} filled ({s.fill_ratio:.1%})")
    print(f"  mean edge capture   {s.mean_edge_capture:>18.2f}")
    print(f"  profitable pairs    {s.profitable_pairs}/{s.pairs_run}")
    if s.per_pair:
        print("\n  top pairs by realized PnL:")
        for d in s.per_pair[:10]:
            print(f"    {d['pair']:<10} {d['realized_pnl']:>14,.2f}  "
                  f"sig={d['signals']:<8,} cap={d['edge_capture_ratio']:>5.2f}  "
                  f"{d['top_reject']}")
    if s.skipped:
        print(f"\n  skipped ({len(s.skipped)}):")
        for d in s.skipped[:12]:
            print(f"    {d['pair']:<10} {d['status']}: {d['detail']}")
    print()


# ── CLI ───────────────────────────────────────────────────────────────────────

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Batch cross-venue arb backtest over many pairs")
    p.add_argument("--pairs", nargs="+", default=list(ALL_PAIRS))
    p.add_argument("--data-root", default="data")
    p.add_argument("--venues", nargs="+", default=[BYBIT, BINANCE])
    p.add_argument("--start", dest="start_date", default=DEFAULT_START_DATE)
    p.add_argument("--end", dest="end_date", default=DEFAULT_END_DATE)
    p.add_argument("--output-dir", default="out/arb_batch")
    p.add_argument("--mode", choices=[TAKER_TAKER, MAKER_TAKER], default=TAKER_TAKER)
    p.add_argument("--latency-preset", choices=sorted(LATENCY_PRESETS),
                   default=DEFAULT_LATENCY_PRESET)
    p.add_argument("--bybit-fees", default="bybit_spot_mm")
    p.add_argument("--binance-fees", default="binance_perp_vip0")
    p.add_argument("--min-edge-bps", type=float, default=1.0)
    p.add_argument("--max-book-age-ms", type=float, default=500.0)
    p.add_argument("--max-touch-age-ms", type=float, default=300.0)
    p.add_argument("--max-clip-notional", type=float, default=25_000.0)
    p.add_argument("--max-events", type=int)
    p.add_argument("--no-deltas", action="store_true")
    p.add_argument("--no-toxic-gate", action="store_true")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--progress-every", type=int, default=0)
    p.add_argument("--allow-rejected-data", metavar="JUSTIFICATION",
                   help="run pairs whose data the quality preflight rejects; recorded per result")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--two-pass", dest="two_pass", action="store_true", default=None)
    g.add_argument("--single-pass", dest="two_pass", action="store_false")
    p.add_argument("--force-two-pass", action="store_true",
                   help="run pass 2 even in taker_taker mode, where it is a no-op")
    p.add_argument(
        "--calibration-days", type=int, default=0, metavar="N",
        help=(
            "static cal/eval split: run a cold N-day calibration pass first, then "
            "evaluate on the remaining days warm-started from those labels. "
            "Superseded by --wf-window-days when both are set."
        ),
    )
    p.add_argument(
        "--wf-window-days", type=int, default=0, metavar="N",
        help=(
            "walk-forward window size in days (0 = disabled). When >0, runs a cold "
            "pass over all days then iterates rolling cal/eval folds of this width."
        ),
    )
    p.add_argument(
        "--wf-step-days", type=int, default=7, metavar="N",
        help="eval window size per walk-forward fold (days); also the fold step size",
    )
    p.add_argument(
        "--min-fill-prob", type=float, default=0.0,
        help="minimum P(fill) gate threshold; 0 disables (maker_taker mode only)",
    )
    p.add_argument(
        "--fill-gate-horizon-s", type=int, default=15,
        help="horizon in seconds used by the fill probability gate",
    )
    p.add_argument(
        "--max-adverse-prob", type=float, default=1.0,
        help=(
            "Stage 4 adverse-selection gate: reject maker leg when "
            "P(price_moved_through) exceeds this threshold (1.0 = disabled). "
            "Requires a FillModelV2 model and maker_taker mode."
        ),
    )
    return p


def main(argv: Iterable[str] | None = None) -> int:
    args = build_arg_parser().parse_args(list(argv) if argv is not None else None)

    # The fill model only matters when there are passive orders to learn from.
    if args.two_pass is None:
        two_pass = args.mode == MAKER_TAKER or args.force_two_pass
    else:
        two_pass = args.two_pass or args.force_two_pass

    base = ArbBacktestConfig(
        data_root=args.data_root, venues=tuple(args.venues),
        start_date=args.start_date, end_date=args.end_date,
        max_events=args.max_events, latency_preset=args.latency_preset,
        bybit_fee_preset=args.bybit_fees, binance_fee_preset=args.binance_fees,
        arb=ArbConfig(
            mode=args.mode,
            min_net_edge_bps=args.min_edge_bps,
            max_book_age_s=args.max_book_age_ms / 1000.0,
            max_touch_age_s=args.max_touch_age_ms / 1000.0,
            max_clip_notional=args.max_clip_notional,
            block_on_toxic=not args.no_toxic_gate,
            min_fill_prob=args.min_fill_prob,
            fill_gate_horizon_s=args.fill_gate_horizon_s,
            max_adverse_prob=args.max_adverse_prob,
        ),
        include_deltas=not args.no_deltas, seed=args.seed,
        progress_every=args.progress_every,
        allow_rejected_data=args.allow_rejected_data,
    )
    run_batch(args.pairs, base, args.output_dir, two_pass=two_pass,
              calibration_days=args.calibration_days,
              wf_window_days=args.wf_window_days,
              wf_step_days=args.wf_step_days)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
