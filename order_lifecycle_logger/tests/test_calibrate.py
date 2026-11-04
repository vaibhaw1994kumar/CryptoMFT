"""Real orders + capture -> fill_events.jsonl in the backtester's schema."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ollog import calibrate  # noqa: E402
from ollog.capture import TardisWriter  # noqa: E402

T0 = 1_793_000_000.0


def _write_run(root: Path):
    w = TardisWriter(root / "capture", "bybit")
    w.write("snapshot", "ETHUSDT", T0 - 10, {"timestamp": T0 - 10, "local_ts": T0 - 10, "exchange": "bybit", "pair": "ETHUSDT", "best_bid": 100.0, "best_ask": 100.5,
                                           "bids": [[100.0, 3.0], [99.5, 10.0]], "asks": [[100.5, 4.0]]})
    w.write("event", "ETHUSDT", T0 - 9.9, {"exchange": "bybit", "pair": "ETHUSDT", "exchange_ts": T0 - 9.9, "local_ts": T0 - 9.9, "side": "ask", "price": 100.6, "volume": 2.0, "event_type": "modify"})
    for i in range(1, 60):                                                                 # the best ask flips 100.5 <-> 100.6 so the mid moves and short_vol is non-zero
        t = T0 - 10 + i * 0.5
        w.write("event", "ETHUSDT", t, {"exchange": "bybit", "pair": "ETHUSDT", "exchange_ts": t, "local_ts": t, "side": "ask", "price": 100.5,
                                        "volume": 0.0 if i % 2 else 4.0, "event_type": "delete" if i % 2 else "modify"})
    for t, side, px in ((T0 - 1.0, "b", 100.5), (T0 - 0.5, "s", 100.0), (T0 + 2.0, "s", 100.0), (T0 + 3.0, "s", 99.5)):   # last one: below the bid
        w.write("trade", "ETHUSDT", t, {"exchange": "bybit", "pair": "ETHUSDT", "exchange_ts": t, "local_ts": t, "price": px, "volume": 1.0, "side": side})
    w.close()
    d = root / "bybit_linear_20261031T000000Z"
    d.mkdir()
    rows = [
        {"kind": "fill_probe", "symbol": "ETHUSDT", "client_id": "c1", "side": "Buy", "price": "100", "qty": "1", "hold_s": 20, "outcome": "filled", "filled_qty": "1",
         "first_fill_ex_ms": int((T0 + 2) * 1000), "last_fill_ex_ms": int((T0 + 2) * 1000), "size_at_price_at_send": 3.0, "t": {},
         "ex": {"created_ms": int(T0 * 1000), "wscan_event_ms": None}},
        {"kind": "fill_probe", "symbol": "ETHUSDT", "client_id": "c2", "side": "Sell", "price": "100.5", "qty": "1", "hold_s": 8, "outcome": "cancelled_unfilled", "filled_qty": "0",
         "first_fill_ex_ms": None, "last_fill_ex_ms": None, "size_at_price_at_send": None, "t": {},
         "ex": {"created_ms": int((T0 + 1) * 1000), "wscan_event_ms": int((T0 + 9) * 1000)}},
        {"kind": "fill_probe", "symbol": "ETHUSDT", "client_id": "c3", "side": "Buy", "price": "100", "qty": "1", "outcome": "rejected_on_stream", "filled_qty": "0", "t": {},
         "ex": {"created_ms": int(T0 * 1000)}},
    ]
    (d / "orders.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    (root / "fillprobe_meta.json").write_text(json.dumps({"network": "testnet"}))


def test_conversion_rows_labels_censoring_and_features(tmp_path):
    _write_run(tmp_path)
    s = calibrate.convert_run(tmp_path)
    rows = [json.loads(x) for x in (tmp_path / "calibration" / "fill_events.jsonl").read_text().splitlines()]
    assert s["rows"] == 8 and s["markets"]["bybit_linear"]["orders"] == 2                 # the rejected order never rested
    a = {r["horizon_s"]: r for r in rows if r["side"] == "bid"}
    b = {r["horizon_s"]: r for r in rows if r["side"] == "ask"}
    assert [a[h]["filled"] for h in (1, 5, 15, 30)] == [False, True, True, True]           # filled 2 s after creation
    assert a[5]["terminal_reason"] == "full_fill" and a[5]["observation_time_s"] == 2.0 and a[5]["fill_ratio"] == 1.0
    assert a[5]["queue_ahead"] == 3.0 and a[5]["relative_to_touch_ticks"] == 0 and a[5]["level_was_absent"] is False
    assert [b[h]["filled"] for h in (1, 5, 15, 30)] == [False] * 4
    assert b[15]["terminal_reason"] == "cancel" and b[15]["observation_time_s"] == 8.0      # cancelled before the 15 s horizon: censorable
    assert b[15]["level_was_absent"] is True and b[15]["queue_ahead"] == 0.0
    assert a[5]["price_moved_through"] is True and a[1]["price_moved_through"] is False    # a sell print at 99.5 at +3 s, below our 100 bid
    assert a[5]["local_depth"] > 0 and a[5]["spread_pct"] > 0 and a[5]["short_vol"] > 0 and a[5]["trade_count"] == 1.0   # one trade in the 2 s window
    assert a[5]["trade_sign"] == 0.0 and a[5]["regime_bucket"] in ("calm", "elevated", "toxic")      # one buy and one sell of 1.0 in the window
    assert "client_id" not in a[5] and a[5]["network"] == "testnet"
    assert s["label_matured_until_ts"] == pytest.approx(T0 + 1 + 30)
    assert s["markets"]["bybit_linear"]["censored"] == {"1": 0, "5": 0, "15": 1, "30": 1}     # the 8 s cancel, at the two longer horizons
    assert s["markets"]["bybit_linear"]["filled_within"] == {"1": 0, "5": 1, "15": 1, "30": 1}


def test_output_is_readable_by_the_backtester_label_extractor(tmp_path):
    src = Path(__file__).resolve().parents[2] / "crypto_MFT_arb_platform" / "src"
    if not (src / "new_backtest").is_dir():
        pytest.skip("needs new_backtest")
    sys.path.insert(0, str(src))
    from new_backtest.fill_label import extract_labels
    _write_run(tmp_path)
    calibrate.convert_run(tmp_path)
    recs = extract_labels(tmp_path / "calibration" / "fill_events.jsonl")
    assert len(recs) == 8
    cens = [r for r in recs if r.is_censored]
    assert sorted(r.horizon_s for r in cens) == [15, 30]                                   # the 8 s cancel is censored at 15 and 30 s only
    assert sum(r.filled for r in recs) == 3
