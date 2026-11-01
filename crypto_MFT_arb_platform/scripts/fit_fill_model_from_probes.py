"""Fit FillModelV2 on REAL perpetual orders (ollog fillprobe runs converted by `ollog calibrate`), so the fill-probability
gate can run on the product it trades instead of on simulated Jan-2022 fills.

    python3.12 scripts/fit_fill_model_from_probes.py --events <run>/calibration/fill_events.jsonl [...] --gate-horizon 5 --out out/fill_model_perp.json

Prints the record counts per horizon and refuses to write a model from fewer than --min-records labels at the gate horizon
(FillModelV2.fit needs both classes present; a tiny sample fits noise). trained_until_ts is set to the latest label maturity, so
the backtester's leakage guard will reject using this model on any start_date before that. Testnet-sourced events are allowed
but the sidecar says so: a testnet fit is a plumbing check, not a mainnet calibration.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src")]

from new_backtest.fill_label import extract_labels  # noqa: E402
from new_backtest.fill_model_v1 import FillModelV2  # noqa: E402


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--events", nargs="+", required=True)
    p.add_argument("--gate-horizon", type=int, default=5)
    p.add_argument("--horizons", default="1,5,15")
    p.add_argument("--min-records", type=int, default=50)
    p.add_argument("--out", required=True)
    a = p.parse_args(argv)
    horizons = tuple(int(x) for x in a.horizons.split(",") if x.strip())
    records, networks = [], Counter()
    for path in a.events:
        recs = extract_labels(path)
        records += recs
        prov = Path(path).parent / "fill_events_provenance.json"
        if prov.exists():
            networks[json.loads(prov.read_text()).get("network") or "unknown"] += len(recs)
        else:
            networks["unknown"] += len(recs)
    by_h = Counter(r.horizon_s for r in records)
    pos = Counter(r.horizon_s for r in records if r.filled)
    cens = Counter(r.horizon_s for r in records if r.is_censored)
    for h in sorted(by_h):
        print(f"horizon {h:>3}s: {by_h[h]:5d} records, {pos[h]:4d} filled, {cens[h]:4d} censored")
    n_gate = by_h.get(a.gate_horizon, 0)
    if n_gate < a.min_records:
        print(f"refusing to fit: {n_gate} records at the gate horizon {a.gate_horizon}s < --min-records {a.min_records}", file=sys.stderr)
        return 2
    until = max(r.timestamp + r.horizon_s for r in records)
    model = FillModelV2.fit(records, gate_horizon_s=a.gate_horizon, trained_until_ts=until, horizons=horizons)
    if model is None:
        print("FillModelV2.fit returned None (single-class labels at some horizon?)", file=sys.stderr)
        return 3
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    model.save_json(out)
    side = {"source": "real orders via ollog fillprobe", "events": a.events, "records": len(records), "by_horizon": dict(by_h),
            "filled_by_horizon": dict(pos), "censored_by_horizon": dict(cens), "networks": dict(networks), "gate_horizon_s": a.gate_horizon,
            "trained_until_ts": until, "calibration_product": "USDT linear perpetual",
            "note": "testnet records are a plumbing check only; use a mainnet-only fit for the gate" if networks.get("testnet") else ""}
    out.with_suffix(".provenance.json").write_text(json.dumps(side, indent=1))
    print(f"wrote {out} (n_train at gate = {getattr(model, 'n_train', '?')}, trained_until_ts {until:.0f}); networks {dict(networks)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
