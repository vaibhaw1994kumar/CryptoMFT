"""Item 3: Compare P&L across maker_max_age_s=0, 15, 30.

Run from the project root:
    cd /Users/vaibhav/Documents/Newproject/crypto_MFT_arb_platform
    python scripts/compare_maker_lifetime.py

Each variant gets its own output directory under out/maker_lifetime_comparison/.
Results are printed side-by-side and saved to out/maker_lifetime_comparison/summary.json.

Purpose: document the full impact of the stale-fill bug (maker_max_age_s=0 allowed
a 515-second fill that produced the spurious +$1.63 P&L on Jan 10 2022).
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

DATA_ROOT   = "/Users/vaibhav/Documents/Newproject/data"
OUTPUT_ROOT = Path("/Users/vaibhav/Documents/Newproject/out/maker_lifetime_comparison")

# Same window as the bug-discovery run (Jan 06-10, 1 WF fold).
START_DATE = "2022-01-06"
END_DATE   = "2022-01-10"

VARIANTS = [
    ("unlimited", 0.0,  "Unlimited — stale fills allowed (pre-fix)"),
    ("ttl_15s",   15.0, "15-second TTL (post-fix default)"),
    ("ttl_30s",   30.0, "30-second TTL"),
]

WF_WINDOW_DAYS = 4
WF_STEP_DAYS   = 1

results = {}

for name, ttl, label in VARIANTS:
    out_dir = OUTPUT_ROOT / name / "SOLUSDT"
    out_dir.mkdir(parents=True, exist_ok=True)

    base_cfg = ArbBacktestConfig(
        data_root=DATA_ROOT,
        venues=("bybit", "binance"),
        pair="SOLUSDT",
        start_date=START_DATE,
        end_date=END_DATE,
        latency_preset="colocated_both",
        bybit_fee_preset="bybit_perp_mm",
        binance_fee_preset="binance_perp_vip0",
        maker=MakerConfig(min_queue_ahead=25.0),
        arb=ArbConfig(
            mode="maker_taker",
            min_net_edge_bps=1.0,
            max_clip_notional=10_000.0,
            block_on_toxic=True,
            min_fill_prob=0.5,
            max_adverse_prob=0.15,
            max_ttf_s=2.5,
            max_cross_venue_skew_s=0.2,
            fill_gate_horizon_s=5,
            maker_max_age_s=ttl,
        ),
        output_dir=str(out_dir),
    )

    print(f"\n{'='*60}")
    print(f"Variant: {label}")
    print(f"{'='*60}")

    outcome = run_pair(
        pair="SOLUSDT",
        base=base_cfg,
        output_root=OUTPUT_ROOT / name,
        two_pass=True,
        wf_window_days=WF_WINDOW_DAYS,
        wf_step_days=WF_STEP_DAYS,
    )

    final = outcome.final or {}
    results[name] = {
        "label": label,
        "maker_max_age_s": ttl,
        "status": outcome.status,
        "signals": final.get("signals_generated", 0),
        "orders_filled": final.get("orders_filled", 0),
        "orders_sent": final.get("orders_sent", 0),
        "realized_pnl": final.get("realized_pnl", 0.0),
        "mean_net_edge_bps": final.get("mean_realized_net_edge_bps", 0.0),
        "wf_folds": final.get("wf_fold_count", "N/A"),
    }

    r = results[name]
    fill_rate = r["orders_filled"] / max(r["orders_sent"], 1) * 100
    print(f"  signals     : {r['signals']}")
    print(f"  fill_rate   : {fill_rate:.1f}% ({r['orders_filled']}/{r['orders_sent']})")
    print(f"  realized_pnl: ${r['realized_pnl']:.4f}")
    print(f"  mean_edge   : {r['mean_net_edge_bps']:.4f} bps")
    print(f"  wf_folds    : {r['wf_folds']}")

print(f"\n{'='*60}")
print("COMPARISON SUMMARY")
print(f"{'='*60}")
print(f"{'Variant':<12} {'TTL':>6} {'Signals':>8} {'PnL':>10} {'Mean edge':>12}")
print("-" * 52)
for name, r in results.items():
    print(f"{name:<12} {r['maker_max_age_s']:>6.0f} {r['signals']:>8} "
          f"${r['realized_pnl']:>9.4f} {r['mean_net_edge_bps']:>11.4f} bps")

out_path = OUTPUT_ROOT / "summary.json"
out_path.parent.mkdir(parents=True, exist_ok=True)
with open(out_path, "w") as fh:
    json.dump(results, fh, indent=2)
print(f"\nSaved: {out_path}")
