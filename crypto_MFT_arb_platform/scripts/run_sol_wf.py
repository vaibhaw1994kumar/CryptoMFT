"""Walk-forward backtest: Bybit/Binance SOLUSDT with fill gate and adverse gate.

Run from the project root:
    cd /Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform
    python scripts/run_sol_wf.py

This script implements Action Items 3 + 4 from the backtest quality report:
  - Walk-forward via run_pair() with wf_window_days=7, wf_step_days=1
  - Fill gate:    min_fill_prob=0.5 (gate signals with low fill probability)
  - Adverse gate: max_adverse_prob=0.15 (block when model predicts adverse fill)
  - block_on_toxic=True (real-time toxic flow detection)
  - Fee fix: bybit_perp_mm (default changed from bybit_spot_mm)

Data requirements:
    data/<venue>/book_snapshots/pair=SOLUSDT/date=<YYYY-MM-DD>/*.jsonl.gz
    data/<venue>/book_events/pair=SOLUSDT/date=<YYYY-MM-DD>/*.jsonl.gz
  for both bybit and binance, covering START_DATE..END_DATE.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from new_backtest.arb_strategy import ArbConfig
from new_backtest.backtester import ArbBacktestConfig
from new_backtest.batch_backtest import run_pair

# ── Configuration ────────────────────────────────────────────────────────────

DATA_ROOT   = "/Users/vaibhav/Documents/Newproject/data"
OUTPUT_ROOT = Path("/Users/vaibhav/Documents/Newproject/out/sol_wf_backtest")

# Date window — extend as data becomes available.
START_DATE = "2022-01-13"
END_DATE   = "2022-01-13"   # single day for now; extend to e.g. "2022-02-13"

BASE_CFG = ArbBacktestConfig(
    data_root=DATA_ROOT,
    venues=("bybit", "binance"),
    pair="SOLUSDT",           # overridden by run_pair
    start_date=START_DATE,
    end_date=END_DATE,
    latency_preset="colocated_both",
    bybit_fee_preset="bybit_perp_mm",     # Action 5: perp fees for both legs
    binance_fee_preset="binance_perp_vip0",
    arb=ArbConfig(
        mode="maker_taker",
        min_net_edge_bps=1.0,
        max_clip_notional=10_000.0,
        # ── Action 4: enable gates ──
        block_on_toxic=True,           # real-time toxic flow block
        min_fill_prob=0.5,             # skip signals with P(fill@5s) < 0.5
        max_adverse_prob=0.15,         # skip signals with P(adverse) > 0.15
        fill_gate_horizon_s=5.0,       # horizon used for both gates
    ),
    output_dir=str(OUTPUT_ROOT / "SOLUSDT"),
)

# ── Walk-forward parameters ───────────────────────────────────────────────────
# Action 3: activate walk-forward via run_pair(), not _run_once().
#
# With only 1 day of data (2022-01-13) the fold loop finds:
#   len(common)=1, wf_window_days=7 → 1 < 7 → falls through to two-pass default.
# To actually exercise the walk-forward, set START_DATE at least 8 days before
# END_DATE, e.g. START_DATE="2022-01-06", END_DATE="2022-02-06".

WF_WINDOW_DAYS = 7    # calibration window: 7 days of fill events → fit model
WF_STEP_DAYS   = 1    # step 1 calendar day forward between folds

# ── Run ──────────────────────────────────────────────────────────────────────

OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

print("=== Walk-forward backtest: Bybit/Binance SOLUSDT ===")
print(f"window : {START_DATE} → {END_DATE}")
print(f"WF     : window={WF_WINDOW_DAYS}d, step={WF_STEP_DAYS}d")
print(f"gates  : min_fill_prob=0.5, max_adverse_prob=0.15, block_on_toxic=True")
print()

outcome = run_pair(
    pair="SOLUSDT",
    base=BASE_CFG,
    output_root=OUTPUT_ROOT,
    two_pass=True,
    wf_window_days=WF_WINDOW_DAYS,
    wf_step_days=WF_STEP_DAYS,
)

print(f"status : {outcome.status}")
print(f"detail : {outcome.detail}")
print()

final = outcome.final
if final:
    sig  = final.get("signals_generated", 0)
    fill = final.get("orders_filled", 0)
    sent = final.get("orders_sent", 1)
    pnl  = final.get("realized_pnl", 0)
    ec   = final.get("edge_capture_ratio", 0)
    edge = final.get("mean_realized_net_edge_bps", 0)
    folds = final.get("wf_fold_count", "N/A (two-pass fallback)")
    print(f"signals     : {sig}")
    print(f"fill_rate   : {fill / max(sent, 1) * 100:.1f}% ({fill}/{sent})")
    print(f"realized_pnl: ${pnl:.4f}")
    print(f"edge_capture: {ec:.4f}")
    print(f"mean_edge   : {edge:.4f} bps")
    print(f"wf_folds    : {folds}")
    print()

    out_path = OUTPUT_ROOT / "summary.json"
    with open(out_path, "w") as fh:
        json.dump({"pair": "SOLUSDT", "status": outcome.status,
                   "detail": outcome.detail, "final": final}, fh, indent=2)
    print(f"Saved: {out_path}")
else:
    print("No result — check data coverage for the requested date window.")
    sys.exit(1)
