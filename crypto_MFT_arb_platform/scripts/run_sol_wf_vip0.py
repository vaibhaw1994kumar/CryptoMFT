"""Walk-forward backtest: conservative fee scenario (bybit_perp_vip0 / binance_perp_vip0).

Same as run_sol_wf.py but with bybit maker fee = 2.0 bps instead of the MM-tier
rebate (-0.5 bps).  This is the realistic fee level before hitting Bybit's MM volume
threshold (~$5M/day).  Compare with run_sol_wf.py to quantify the MM-tier uplift.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from new_backtest.arb_strategy import ArbConfig
from new_backtest.backtester import ArbBacktestConfig
from new_backtest.batch_backtest import run_pair
from new_backtest.fill_engine import MakerConfig

# ── Configuration ────────────────────────────────────────────────────────────

DATA_ROOT   = "/Users/vaibhav/Documents/Newproject/data"
OUTPUT_ROOT = Path("/Users/vaibhav/Documents/Newproject/out/sol_wf_backtest_vip0")

_FUNDING_DIR = Path("/Users/vaibhav/Documents/Newproject/out/funding/SOLUSDT")
FUNDING_DIR: str | None = str(_FUNDING_DIR) if _FUNDING_DIR.exists() else None

START_DATE = "2022-01-03"
END_DATE   = "2022-01-31"

BASE_CFG = ArbBacktestConfig(
    data_root=DATA_ROOT,
    venues=("bybit", "binance"),
    pair="SOLUSDT",
    start_date=START_DATE,
    end_date=END_DATE,
    latency_preset="colocated_both",
    bybit_fee_preset="bybit_perp_vip0",      # conservative: 2.0 bps maker (no MM rebate)
    binance_fee_preset="binance_perp_vip0",   # same as optimistic run
    maker=MakerConfig(min_queue_ahead=25.0),
    arb=ArbConfig(
        mode="maker_taker",
        min_net_edge_bps=1.0,
        max_clip_notional=10_000.0,
        max_position_base=1.0,
        block_on_toxic=True,
        min_fill_prob=0.5,
        max_adverse_prob=0.15,
        max_ttf_s=2.5,
        max_cross_venue_skew_s=0.2,
        fill_gate_horizon_s=5,
        maker_max_age_s=15.0,
    ),
    output_dir=str(OUTPUT_ROOT / "SOLUSDT"),
    funding_rate_dir=FUNDING_DIR,
)

WF_WINDOW_DAYS = 4
WF_STEP_DAYS   = 1

OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

print("=== Walk-forward backtest: Bybit/Binance SOLUSDT [VIP0 fees] ===")
print(f"window : {START_DATE} → {END_DATE}")
print(f"WF     : window={WF_WINDOW_DAYS}d, step={WF_STEP_DAYS}d")
print(f"fees   : bybit_perp_vip0 (2.0 bps maker) / binance_perp_vip0")
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
    funding = final.get("funding_paid", 0)
    print(f"signals     : {sig}")
    print(f"fill_rate   : {fill / max(sent, 1) * 100:.1f}% ({fill}/{sent})")
    print(f"realized_pnl: ${pnl:.4f}")
    print(f"funding_paid: ${funding:.4f}")
    print(f"edge_capture: {ec:.4f}")
    print(f"mean_edge   : {edge:.4f} bps")
    print(f"wf_folds    : {folds}")
    print()

    out_path = OUTPUT_ROOT / "summary.json"
    with open(out_path, "w") as fh:
        json.dump({"pair": "SOLUSDT", "fee_scenario": "vip0",
                   "status": outcome.status, "detail": outcome.detail,
                   "final": final}, fh, indent=2)
    print(f"Saved: {out_path}")
else:
    print("No result — check data coverage for the requested date window.")
    sys.exit(1)
