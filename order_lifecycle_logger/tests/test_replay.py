"""Replay scoring, order loading, and an end-to-end check of the injection harness on a tiny synthetic capture."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ollog import replay  # noqa: E402
from ollog.capture import TardisWriter  # noqa: E402

SRC = Path(__file__).resolve().parents[2] / "crypto_MFT_arb_platform" / "src"
T0 = 1_793_000_000.0


def row(real_filled, first, sim_filled, sim_first):
    return {"real_filled": real_filled, "first_fill": first, "sim_filled": sim_filled, "sim_first": sim_first, "qty": 1.0}


def test_classification_uses_a_one_second_window_around_the_real_first_fill():
    assert replay.classify(row(1, 100.0, 1, 100.9)) == "hit"
    assert replay.classify(row(1, 100.0, 1, 99.0 + 0.0)) == "hit"          # exactly 1 s early is still a hit
    assert replay.classify(row(1, 100.0, 1, 98.9)) == "premature"
    assert replay.classify(row(1, 100.0, 1, 101.1)) == "late"
    assert replay.classify(row(1, 100.0, 0, None)) == "unfilled"
    assert replay.classify(row(0, None, 1, 100.0)) == "false_fill"            # the simulation filled an order that really did not
    assert replay.classify(row(0, None, 0, None)) == "true_unfilled"


def test_score_splits_real_filled_from_real_unfilled_and_reports_wilson_intervals():
    rows = [row(1, 10.0, 1, 10.2), row(1, 10.0, 1, 5.0), row(1, 10.0, 0, None), row(0, None, 1, 3.0), row(0, None, 0, None), row(0, None, 0, None)]
    s = replay.score(rows)
    assert s["real_filled_orders"] == 3 and s["real_unfilled_orders"] == 3
    assert s["hit"]["share"] == pytest.approx(1 / 3) and s["premature"]["share"] == pytest.approx(1 / 3) and s["unfilled"]["share"] == pytest.approx(1 / 3)
    assert s["false_fill"]["share"] == pytest.approx(1 / 3)
    lo, hi = s["hit"]["ci95"]
    assert 0 < lo < 1 / 3 < hi < 1


def test_wilson_interval_known_values():
    lo, hi = replay.wilson(5, 10)
    assert lo == pytest.approx(0.2366, abs=1e-3) and hi == pytest.approx(0.7634, abs=1e-3)
    assert replay.wilson(0, 0)[0] != replay.wilson(0, 0)[0]                  # nan for no data


def _probe(run: Path, market: str, rec: dict):
    d = run / f"{market}_20261031T000000Z"
    d.mkdir(parents=True, exist_ok=True)
    with (d / "orders.jsonl").open("a") as f:
        f.write(json.dumps({"kind": "fill_probe", "symbol": "ETHUSDT", "client_id": "c1", "side": "Buy", "price": "100", "qty": "1",
                            "hold_s": 5, "t": {}, **rec}) + "\n")


def test_load_orders_end_time_and_exclusions(tmp_path):
    full = {"outcome": "filled", "filled_qty": "1", "first_fill_ex_ms": int((T0 + 2) * 1000), "last_fill_ex_ms": int((T0 + 3) * 1000),
            "ex": {"created_ms": int(T0 * 1000), "wscan_event_ms": None}}
    cancelled = {"outcome": "cancelled_unfilled", "filled_qty": "0", "first_fill_ex_ms": None, "last_fill_ex_ms": None,
                 "ex": {"created_ms": int(T0 * 1000), "wscan_event_ms": int((T0 + 9) * 1000)}, "client_id": "c2"}
    rejected = {"outcome": "rejected_on_stream", "filled_qty": "0", "ex": {"created_ms": int(T0 * 1000)}, "client_id": "c3"}
    for r in (full, cancelled, rejected):
        _probe(tmp_path, "bybit_linear", r)
    os_ = replay.load_orders(tmp_path)
    assert len(os_) == 2                                                    # a rejected order never rested
    a, b = sorted(os_, key=lambda o: o["order_id"])
    assert a["end"] == T0 + 3 and a["first_fill"] == T0 + 2 and a["venue"] == "bybit"      # fully filled: ends at its last fill
    assert b["end"] == T0 + 9 and b["real_filled"] == 0                      # unfilled: ends at the exchange's cancel event


def _write_capture(root: Path, trade_qty: float | None):
    w = TardisWriter(root, "bybit")
    rec = (int(T0 * 1e9), 1)
    w.write("snapshot", "ETHUSDT", T0, {"timestamp": T0, "local_ts": T0 + 0.01, "exchange": "bybit", "pair": "ETHUSDT", "best_bid": 100.0, "best_ask": 100.5,
                                        "bids": [[100.0, 3.0], [99.5, 10.0]], "asks": [[100.5, 4.0]]})
    # unrelated churn so the book stays fresh and the clock advances past the order's life
    for i in range(1, 30):
        w.write("event", "ETHUSDT", T0 + i, {"exchange": "bybit", "pair": "ETHUSDT", "exchange_ts": T0 + i, "local_ts": T0 + i + 0.01, "side": "ask",
                                             "price": 100.5, "volume": 4.0 + (i % 2), "event_type": "modify"})
    if trade_qty:
        w.write("trade", "ETHUSDT", T0 + 5.0, {"exchange": "bybit", "pair": "ETHUSDT", "exchange_ts": T0 + 5.0, "local_ts": T0 + 5.01, "price": 100.0,
                                               "volume": trade_qty, "side": "s"})
    w.close()


def _order(day):
    return {"market": "bybit_linear", "venue": "bybit", "symbol": "ETHUSDT", "day": day, "order_id": "x", "side": "Buy", "price": 100.0, "qty": 1.0,
            "created": T0 + 1.0, "real_filled": 0.0, "first_fill": None, "last_fill": None, "end": T0 + 20.0, "outcome": "x", "queue_at_send": 3.0}


@pytest.mark.skipif(not (SRC / "new_backtest").is_dir(), reason="needs a new_backtest source tree")
def test_harness_end_to_end_a_real_trade_through_the_queue_fills_and_no_trade_does_not(tmp_path):
    from datetime import datetime, timezone
    day = datetime.fromtimestamp(T0, timezone.utc).strftime("%Y-%m-%d")
    nb = replay._load_nb(str(SRC))
    _write_capture(tmp_path / "a", trade_qty=50.0)                           # sell volume of 50 at our price: clears the 3 ahead and fills our 1
    rows = replay.replay_one(nb, str(tmp_path / "a"), "bybit", "ETHUSDT", day, [_order(day)], {}, 0.0)
    assert rows[0]["sim_filled"] == pytest.approx(1.0) and rows[0]["sim_first"] == pytest.approx(T0 + 5.0, abs=0.5)
    assert rows[0]["sim_queue_at_land"] == pytest.approx(3.0)                # queue ahead taken from the replayed book at the order's creation
    _write_capture(tmp_path / "b", trade_qty=None)
    rows = replay.replay_one(nb, str(tmp_path / "b"), "bybit", "ETHUSDT", day, [_order(day)], {}, 0.0)
    assert rows[0]["sim_filled"] == 0.0


def test_recon_summary_counts_by_venue(tmp_path):
    (tmp_path / "recon_checks.jsonl").write_text("\n".join(json.dumps(x) for x in [
        {"venue": "bybit", "aligned": True, "match": True, "top1_match": True}, {"venue": "bybit", "aligned": True, "match": False, "top1_match": True},
        {"venue": "binance", "aligned": False}]))
    s = replay.recon_summary(tmp_path)
    assert s["bybit"] == {"checks": 2, "aligned": 2, "top_n_exact_match": 1, "top1_match": 2} and s["binance"]["aligned"] == 0


@pytest.mark.skipif(not (SRC / "new_backtest").is_dir(), reason="needs a new_backtest source tree")
def test_harness_cancels_at_the_real_end_and_ignores_trades_before_creation(tmp_path):
    from datetime import datetime, timezone
    day = datetime.fromtimestamp(T0, timezone.utc).strftime("%Y-%m-%d")
    nb = replay._load_nb(str(SRC))
    _write_capture(tmp_path / "a", trade_qty=50.0)                           # the only trade is at T0+5
    early_end = dict(_order(day), end=T0 + 3.0)                              # the real order was cancelled before that trade: no fill possible
    assert replay.replay_one(nb, str(tmp_path / "a"), "bybit", "ETHUSDT", day, [early_end], {}, 0.0)[0]["sim_filled"] == 0.0
    late_create = dict(_order(day), created=T0 + 8.0, end=T0 + 25.0)         # created after the trade: that trade cannot fill it
    assert replay.replay_one(nb, str(tmp_path / "a"), "bybit", "ETHUSDT", day, [late_create], {}, 0.0)[0]["sim_filled"] == 0.0
    assert replay.replay_one(nb, str(tmp_path / "a"), "bybit", "ETHUSDT", day, [early_end], {}, 5.0)[0]["sim_filled"] == pytest.approx(1.0)   # grace keeps it alive
