"""Phase D, step D5 (owner 9 Nov): timing is a RULE, the same on both runtimes. The same synthetic files go through v1_4 on the backtester's loop and v1_5 on the session; order by order, decision,
send, arrival and fill times agree within 1 ms (profile timing: latency preset, the 35 ms Binance floor, truth books at arrival, fill-report latency). Wiring only, never a profit figure."""
import dataclasses
import gzip
import json
import sys
import tempfile
import unittest
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from new_backtest.backtester import CrossVenueArbBacktester
from new_backtest.profiles import apply_profile, load_profile
from new_backtest.session_replay import SessionReplay, compare_order_times, order_times_new, order_times_old

T0 = 1710115200.0           # 2024-03-11 00:00:00 UTC
X = Path(__file__).resolve().parents[2] / "out" / "queue_v2"


def write_dataset(root: Path) -> None:
    for venue, (bid, ask) in (("bybit", (100.00, 100.30)), ("binance", (100.30, 100.31))):
        d = root / venue / "book_snapshots" / "pair=SOLUSDT" / "date=2024-03-11"
        d.mkdir(parents=True)
        with gzip.open(d / "1.jsonl.gz", "wt") as fh:
            for i in range(161):                                           # both books fresh every 50 ms (the preflight wants at least 100 events)
                t = T0 + 3600 + 1.0 + 0.05 * i
                fh.write(json.dumps({"timestamp": t, "local_ts": t + 0.002, "bids": [[bid, 50], [round(bid - 0.01, 2), 50]], "asks": [[ask, 50], [round(ask + 0.01, 2), 50]]}) + "\n")
    d = root / "bybit" / "trades" / "pair=SOLUSDT" / "date=2024-03-11"
    d.mkdir(parents=True)
    with gzip.open(d / "1.jsonl.gz", "wt") as fh:
        t = T0 + 3600 + 3.0
        fh.write(json.dumps({"exchange_ts": t, "local_ts": t + 0.002, "price": 100.24, "volume": 30, "side": "sell", "trade_id": "t1"}) + "\n")      # a sell print through the resting bid


def config(name: str, root: Path):
    profile = load_profile(name)
    cfg = apply_profile(profile, dataset="mar2024", data_root=str(root), pair="SOLUSDT", start_date="2024-03-11", end_date="2024-03-11", output_dir=str(root / ("out_" + name)),
                        funding_rate_dir=None, max_trade_log=10 ** 7, equity_bridge="warn", allow_manifest_gap="synthetic wiring test", record_order_events=True)
    cfg.maker = dataclasses.replace(cfg.maker, unknown_queue_fit=str(X / "unknown_queue_fit_mar2024_NONE.json"))
    cfg.arb.max_position_base = 1.0
    return profile, cfg


@unittest.skipUnless((X / "unknown_queue_fit_mar2024_NONE.json").exists(), "queue fit file not available")
class SameTimingOnBothRuntimes(unittest.TestCase):
    def test_every_order_has_the_same_decision_send_arrival_and_fill_times(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_dataset(root)
            _, cfg4 = config("perp_perp_maker_taker_v1_4", root)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                bt = CrossVenueArbBacktester(cfg4)
                bt.run()
            profile5, cfg5 = config("perp_perp_maker_taker_v1_5", root)
            replay = SessionReplay(cfg5, profile5)
            replay.run()
            old, new = order_times_old(bt.events.events), order_times_new(replay)
        self.assertEqual(len(old), 2)
        self.assertEqual(len(new), len(old))
        rows = compare_order_times(old, new)
        bad = [(r["order"], k, v) for r in rows for k, v in r.items() if isinstance(v, dict) and v.get("within_tolerance") is False]
        self.assertEqual(bad, [], f"timing differs: {bad}")
        self.assertEqual([(r["venue"], r["side"]) for r in rows], [(("bybit", "bybit"), ("buy", "buy")), (("binance", "binance"), ("sell", "sell"))])
        self.assertGreaterEqual(replay.timing_stats["truth_arrivals"], 1)                 # the hedge executed on the venue-truth book at its arrival
        self.assertGreater(new[1]["send"] - new[0]["arrival"], 1.0)


if __name__ == "__main__":
    unittest.main()
