"""Sensitivity sweep: how do queue model assumptions affect maker fill rates and PnL?

Three knobs in MakerConfig determine how aggressively the simulator awards fills
to resting passive orders:

  queue_stress_multiplier  (default 1.0)
      Initial queue_ahead = observed_level_size × this factor.
      1.0 = assume we are last in a pro-rata queue behind all resting size.
      0.0 = assume we skip the queue (FIFO-improved price, first in).
      2.0 = pessimistic: assume there is 2× the visible size ahead of us.

  trade_fill_share  (default 1.00)
      Fraction of a crossing trade's volume that drains our queue/fills our order.
      1.0 = we get every last lot that crosses our price; 0.5 = half of it.

  delta_fill_share  (default 0.15)
      Fraction of a negative book-level delta awarded to us.  Level shrinks because
      someone cancelled ahead of us — a fraction of that cancellation moves us up
      the queue and eventually fills us.

Because these knobs interact with the EmpiricalFillModel that learns from the run's
own outcomes, the sweep is run in two modes:

  cold   pass 1 only (no warm-start, no learned priors) — cleanest single-knob signal.
  warm   two-pass — pass 1 trains the model, pass 2 uses it; shows whether learned
         priors amplify or dampen the assumption's effect.

Outputs (per setting) stored in SweepPoint:
  orders_filled, orders_sent, fill_rate (filled/sent), signals_completed,
  edge_capture_ratio, realized_pnl, mean_realized_net_edge_bps

Usage (CLI)::

    python -m new_backtest.queue_sensitivity \\
        --pair BTCUSDT \\
        --data-root data \\
        --start 2024-01-01 --end 2024-01-31 \\
        --output out/queue_sweep.json

Or programmatically::

    from new_backtest.queue_sensitivity import sweep
    report = sweep(base_config, pair, output_dir=Path("out/sweep"))
    report.print_table()
"""
from __future__ import annotations

import json
import textwrap
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Sequence

from .arb_strategy import ArbConfig, MAKER_TAKER
from .backtester import ArbBacktestConfig
from .batch_backtest import _clone, _date_to_epoch, _run_once
from .config import BYBIT, BINANCE, DEFAULT_LATENCY_PRESET
from .fill_engine import MakerConfig


# ── Result types ─────────────────────────────────────────────────────────────

@dataclass
class SweepPoint:
    """Metrics from one backtester run with a specific MakerConfig setting."""
    # Knob values
    queue_stress_multiplier: float
    trade_fill_share: float
    delta_fill_share: float
    min_queue_ahead: float = 0.0

    # Derived from backtest result
    orders_sent: int = 0
    orders_filled: int = 0
    fill_rate: float = 0.0          # orders_filled / orders_sent (0 when sent==0)
    signals_generated: int = 0
    signals_completed: int = 0
    edge_capture_ratio: float = 0.0
    realized_pnl: float = 0.0
    mean_realized_net_edge_bps: float = 0.0
    mean_ttf_s: float = 0.0         # from maker_stats["mean_time_to_fill_s"] if present
    status: str = "ok"
    detail: str = ""


@dataclass
class QueueSensitivityReport:
    pair: str
    start_date: str
    end_date: str
    latency_preset: str
    mode: str

    # Baseline point with default MakerConfig (queue_stress_multiplier=1, shares=default)
    baseline: SweepPoint

    # queue_stress_multiplier sweep (trade/delta shares fixed at default)
    queue_stress_sweep: list[SweepPoint] = field(default_factory=list)

    # trade_fill_share sweep (queue_stress=1, delta=default)
    trade_fill_sweep: list[SweepPoint] = field(default_factory=list)

    # delta_fill_share sweep (queue_stress=1, trade=default)
    delta_fill_sweep: list[SweepPoint] = field(default_factory=list)

    # min_queue_ahead sweep (queue_stress=1, trade/delta at default)
    min_queue_ahead_sweep: list[SweepPoint] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, default=str)

    def print_table(self) -> None:
        """Print a concise summary table to stdout."""
        print(f"\n{'='*72}")
        print(f"Queue Sensitivity  {self.pair}  {self.start_date}..{self.end_date}  "
              f"mode={self.mode}  latency={self.latency_preset}")
        print(f"{'='*72}")

        def _row(pts: list[SweepPoint], knob_label: str, knob_getter) -> None:
            print(f"\n  {knob_label}")
            print(f"  {'value':>6}  {'sent':>6}  {'filled':>6}  "
                  f"{'fill%':>6}  {'cap':>5}  {'pnl':>10}  {'net_bps':>7}  {'ttf_s':>6}")
            print(f"  {'-'*70}")
            for p in pts:
                tag = " *" if p is self.baseline else ""
                print(f"  {knob_getter(p):>6.2f}  {p.orders_sent:>6,}  "
                      f"{p.orders_filled:>6,}  {100*p.fill_rate:>5.1f}%  "
                      f"{p.edge_capture_ratio:>5.2f}  {p.realized_pnl:>10,.2f}  "
                      f"{p.mean_realized_net_edge_bps:>7.3f}  {p.mean_ttf_s:>6.1f}"
                      f"{tag}")

        _row(self.queue_stress_sweep, "queue_stress_multiplier",
             lambda p: p.queue_stress_multiplier)
        _row(self.trade_fill_sweep, "trade_fill_share",
             lambda p: p.trade_fill_share)
        _row(self.delta_fill_sweep, "delta_fill_share",
             lambda p: p.delta_fill_share)
        _row(self.min_queue_ahead_sweep, "min_queue_ahead",
             lambda p: p.min_queue_ahead)
        print()


# ── Core sweep function ───────────────────────────────────────────────────────

def _run_point(
    base: ArbBacktestConfig,
    pair: str,
    out_dir: Path,
    maker_cfg: MakerConfig,
    label: str,
) -> SweepPoint:
    """Run one backtest with the given MakerConfig and extract SweepPoint."""
    from dataclasses import replace  # noqa: PLC0415 — stdlib, no weight
    cfg = deepcopy(base)
    cfg = ArbBacktestConfig(
        pair=cfg.pair, data_root=cfg.data_root, venues=cfg.venues,
        start_date=cfg.start_date, end_date=cfg.end_date,
        max_events=cfg.max_events, latency_preset=cfg.latency_preset,
        bybit_fee_preset=cfg.bybit_fee_preset, binance_fee_preset=cfg.binance_fee_preset,
        arb=cfg.arb, maker=maker_cfg, venue_state=cfg.venue_state,
        consume_liquidity=cfg.consume_liquidity, include_deltas=cfg.include_deltas,
        seed=cfg.seed, agent_flow=cfg.agent_flow,
        output_dir=str(out_dir / label),
        warm_start_fill_events=cfg.warm_start_fill_events,
        warm_start_until_ts=cfg.warm_start_until_ts,
    )

    sp = SweepPoint(
        queue_stress_multiplier=maker_cfg.queue_stress_multiplier,
        trade_fill_share=maker_cfg.trade_fill_share,
        delta_fill_share=maker_cfg.delta_fill_share,
        min_queue_ahead=maker_cfg.min_queue_ahead,
    )

    try:
        r = _run_once(cfg)
        sp.orders_sent = r.orders_sent
        sp.orders_filled = r.orders_filled
        sp.fill_rate = r.orders_filled / r.orders_sent if r.orders_sent > 0 else 0.0
        sp.signals_generated = r.signals_generated
        sp.signals_completed = r.signals_completed
        sp.edge_capture_ratio = r.edge_capture_ratio
        sp.realized_pnl = r.realized_pnl
        sp.mean_realized_net_edge_bps = r.mean_realized_net_edge_bps
        sp.mean_ttf_s = r.maker_stats.get("mean_time_to_fill_s", 0.0)
        sp.detail = str(r.start_date)
    except Exception as exc:
        sp.status = "error"
        sp.detail = f"{type(exc).__name__}: {exc}"
    return sp


def sweep(
    base: ArbBacktestConfig,
    pair: str,
    output_dir: Path | str | None = None,
    queue_stress_values: Sequence[float] = (0.0, 0.25, 0.5, 1.0, 2.0, 5.0),
    trade_fill_values: Sequence[float] = (0.5, 0.75, 1.0),
    delta_fill_values: Sequence[float] = (0.05, 0.15, 0.30),
    min_queue_ahead_values: Sequence[float] = (0.0, 10.0, 25.0, 38.5, 100.0, 188.0),
) -> QueueSensitivityReport:
    """Run the sensitivity sweep and return a QueueSensitivityReport.

    Parameters
    ----------
    base:
        Template ArbBacktestConfig. Its pair/output_dir are ignored; use the
        `pair` argument for pair and `output_dir` for output location.
    pair:
        Single pair to sweep (e.g. "BTCUSDT").
    output_dir:
        Directory for per-run output dirs. Created if absent. If None, a
        temporary directory is used.
    queue_stress_values:
        Values of MakerConfig.queue_stress_multiplier to sweep.  Must include 1.0
        for the baseline.
    min_queue_ahead_values:
        Values of MakerConfig.min_queue_ahead to sweep (queue_stress=1,
        trade/delta shares at default). 0.0 = current default (no floor).
        Calibrate from the p25 of observed present-level depths per instrument.
    trade_fill_values:
        Values of MakerConfig.trade_fill_share to sweep. Must include 1.0 for baseline.
    delta_fill_values:
        Values of MakerConfig.delta_fill_share to sweep. Must include 0.15 for baseline.
    """
    import tempfile  # noqa: PLC0415
    if output_dir is None:
        _tmp = tempfile.mkdtemp()
        out = Path(_tmp)
    else:
        out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    default_mc = MakerConfig()

    # Clone base for this pair (uses batch_backtest helpers for consistent setup)
    from .batch_backtest import _date_to_epoch, _run_once  # noqa: PLC0415

    # Baseline run
    baseline_mc = MakerConfig()
    baseline_sp = _run_point(base, pair, out, baseline_mc, "baseline")

    # queue_stress sweep (trade/delta at default)
    qs_sweep: list[SweepPoint] = []
    for qs in sorted(set(queue_stress_values)):
        mc = MakerConfig(
            queue_stress_multiplier=qs,
            trade_fill_share=default_mc.trade_fill_share,
            delta_fill_share=default_mc.delta_fill_share,
            snapshot_fill_share=default_mc.snapshot_fill_share,
        )
        label = f"qs_{qs:.2f}".replace(".", "p")
        sp = _run_point(base, pair, out, mc, label)
        if abs(qs - 1.0) < 1e-9:
            sp = baseline_sp  # reuse, avoid duplicate run
        qs_sweep.append(sp)

    # trade_fill sweep (queue_stress=1, delta=default)
    tf_sweep: list[SweepPoint] = []
    for tf in sorted(set(trade_fill_values)):
        mc = MakerConfig(
            queue_stress_multiplier=default_mc.queue_stress_multiplier,
            trade_fill_share=tf,
            delta_fill_share=default_mc.delta_fill_share,
            snapshot_fill_share=default_mc.snapshot_fill_share,
        )
        label = f"tf_{tf:.2f}".replace(".", "p")
        sp = _run_point(base, pair, out, mc, label)
        if abs(tf - 1.0) < 1e-9:
            sp = baseline_sp
        tf_sweep.append(sp)

    # delta_fill sweep (queue_stress=1, trade=default)
    df_sweep: list[SweepPoint] = []
    for df in sorted(set(delta_fill_values)):
        mc = MakerConfig(
            queue_stress_multiplier=default_mc.queue_stress_multiplier,
            trade_fill_share=default_mc.trade_fill_share,
            delta_fill_share=df,
            snapshot_fill_share=default_mc.snapshot_fill_share,
        )
        label = f"df_{df:.2f}".replace(".", "p")
        sp = _run_point(base, pair, out, mc, label)
        if abs(df - default_mc.delta_fill_share) < 1e-9:
            sp = baseline_sp
        df_sweep.append(sp)

    # min_queue_ahead sweep (queue_stress=1, trade/delta at default)
    mqa_sweep: list[SweepPoint] = []
    for mqa in sorted(set(min_queue_ahead_values)):
        mc = MakerConfig(
            queue_stress_multiplier=default_mc.queue_stress_multiplier,
            trade_fill_share=default_mc.trade_fill_share,
            delta_fill_share=default_mc.delta_fill_share,
            snapshot_fill_share=default_mc.snapshot_fill_share,
            min_queue_ahead=mqa,
        )
        label = f"mqa_{mqa:.1f}".replace(".", "p")
        sp = _run_point(base, pair, out, mc, label)
        if abs(mqa - 0.0) < 1e-9:
            sp = baseline_sp  # 0.0 is the default → reuse baseline
        mqa_sweep.append(sp)

    report = QueueSensitivityReport(
        pair=pair,
        start_date=base.start_date,
        end_date=base.end_date,
        latency_preset=base.latency_preset,
        mode=base.arb.mode,
        baseline=baseline_sp,
        queue_stress_sweep=qs_sweep,
        trade_fill_sweep=tf_sweep,
        delta_fill_sweep=df_sweep,
        min_queue_ahead_sweep=mqa_sweep,
    )

    if output_dir is not None:
        (out / "queue_sensitivity_report.json").write_text(
            report.to_json(), encoding="utf-8",
        )

    return report


# ── CLI ───────────────────────────────────────────────────────────────────────

def _build_parser():
    import argparse  # noqa: PLC0415
    p = argparse.ArgumentParser(
        description=textwrap.dedent("""\
            Sweep MakerConfig queue assumptions and report fill rate / PnL sensitivity.

            Varies queue_stress_multiplier, trade_fill_share, delta_fill_share
            one at a time and runs the arb backtester for each setting.
            Writes queue_sensitivity_report.json to --output.
        """)
    )
    p.add_argument("--pair", default="BTCUSDT")
    p.add_argument("--data-root", default="data")
    p.add_argument("--venues", nargs="+", default=[BYBIT, BINANCE])
    p.add_argument("--start", dest="start_date", default="2024-01-01")
    p.add_argument("--end", dest="end_date", default="2024-01-07")
    p.add_argument("--output", default="out/queue_sensitivity")
    p.add_argument("--latency-preset", default=DEFAULT_LATENCY_PRESET)
    p.add_argument("--mode", choices=[MAKER_TAKER], default=MAKER_TAKER,
                   help="Only maker_taker produces passive orders to sweep")
    p.add_argument("--min-edge-bps", type=float, default=1.0)
    p.add_argument("--max-clip-notional", type=float, default=25_000.0)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument(
        "--queue-stress-values", nargs="+", type=float,
        default=[0.0, 0.25, 0.5, 1.0, 2.0, 5.0],
        metavar="F",
        help="Values for queue_stress_multiplier (default: 0.0 0.25 0.5 1.0 2.0 5.0)",
    )
    p.add_argument(
        "--trade-fill-values", nargs="+", type=float,
        default=[0.5, 0.75, 1.0],
        metavar="F",
        help="Values for trade_fill_share (default: 0.5 0.75 1.0)",
    )
    p.add_argument(
        "--delta-fill-values", nargs="+", type=float,
        default=[0.05, 0.15, 0.30],
        metavar="F",
        help="Values for delta_fill_share (default: 0.05 0.15 0.30)",
    )
    p.add_argument(
        "--min-queue-ahead-values", nargs="+", type=float,
        default=[0.0, 10.0, 25.0, 38.5, 100.0, 188.0],
        metavar="F",
        dest="min_queue_ahead_values",
        help="Values for min_queue_ahead floor in base-asset units "
             "(default: 0.0 10.0 25.0 38.5 100.0 188.0 — calibrated from "
             "SOLUSDT 2022-01-13 present-level p10/p25/p50/mean)",
    )
    return p


def main(argv=None):
    import sys  # noqa: PLC0415
    from .arb_strategy import ArbConfig  # noqa: PLC0415
    from .backtester import ArbBacktestConfig  # noqa: PLC0415
    from .config import max_position_for  # noqa: PLC0415

    args = _build_parser().parse_args(sys.argv[1:] if argv is None else list(argv))
    base = ArbBacktestConfig(
        data_root=args.data_root,
        venues=tuple(args.venues),
        start_date=args.start_date,
        end_date=args.end_date,
        latency_preset=args.latency_preset,
        arb=ArbConfig(
            mode=args.mode,
            min_net_edge_bps=args.min_edge_bps,
            max_clip_notional=args.max_clip_notional,
            max_position_base=max_position_for(args.pair),
            block_on_toxic=False,
        ),
        seed=args.seed,
    )
    report = sweep(
        base, args.pair,
        output_dir=args.output,
        queue_stress_values=args.queue_stress_values,
        trade_fill_values=args.trade_fill_values,
        delta_fill_values=args.delta_fill_values,
        min_queue_ahead_values=args.min_queue_ahead_values,
    )
    report.print_table()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
