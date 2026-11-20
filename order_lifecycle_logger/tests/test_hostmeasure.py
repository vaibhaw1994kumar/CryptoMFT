"""ollog hostmeasure (owner 20 Nov, Phase 2 F03 d): the recording loops with injected runners/clients and the derive step on a constructed run. Offline, no network.
The chrony text below is CONSTRUCTED from the documented `chronyc tracking` format (UNVERIFIED until a real run on the production host)."""
import asyncio
import gzip
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
SRC = str(Path(__file__).resolve().parents[2] / "crypto_MFT_arb_platform" / "src")

from ollog import hostmeasure as hm  # noqa: E402

T0 = 1_793_000_000_000_000_000           # wall ns of the constructed run


def chrony_text(offset_s: float, fast: bool, ref_epoch_s: int, disp_s: float = 0.0005, delay_s: float = 0.002) -> str:
    import datetime as dt
    ref = dt.datetime.fromtimestamp(ref_epoch_s, dt.timezone.utc).strftime("%a %b %d %H:%M:%S %Y")
    return (f"Reference ID    : A9FEA97B (169.254.169.123)\nStratum         : 4\nRef time (UTC)  : {ref}\n"
            f"System time     : {offset_s:.9f} seconds {'fast' if fast else 'slow'} of NTP time\nLast offset     : -0.000001234 seconds\n"
            f"RMS offset      : 0.000004321 seconds\nFrequency       : 12.345 ppm slow\nResidual freq   : +0.001 ppm\nSkew            : 0.012 ppm\n"
            f"Root delay      : {delay_s:.9f} seconds\nRoot dispersion : {disp_s:.9f} seconds\nUpdate interval : 64.5 seconds\nLeap status     : Normal\n")


def test_chrony_loop_records_raw_text_and_a_broken_binary():
    rows = []

    async def go(runner):
        stop = asyncio.Event()
        asyncio.get_running_loop().call_later(0.05, stop.set)
        return await hm.chrony_loop(rows.append, stop, 0.01, runner)
    asyncio.run(go(lambda: (0, chrony_text(0.001, True, T0 // 10**9), "")))
    assert rows and rows[0]["rc"] == 0 and "System time" in rows[0]["text"] and rows[0]["t_mono_ns"] > 0
    rows.clear()
    asyncio.run(go(lambda: hm.run_chronyc("/nonexistent/chronyc")))
    assert rows and rows[0]["rc"] is None and "FileNotFoundError" in rows[0]["stderr"]


def test_rest_loop_records_round_trip_and_server_time():
    class C:
        cfg = {"rest": "https://api.bybit.com"}

        async def _http(self, method, url):
            assert url == "https://api.bybit.com/v5/market/time"
            return 200, {"retCode": 0, "result": {"timeNano": "1793000000123456789"}, "time": 1793000000123}, (T0, 10), (T0 + 4_000_000, 4_000_010)
    rows = []

    async def go():
        stop = asyncio.Event()
        asyncio.get_running_loop().call_later(0.03, stop.set)
        return await hm.rest_loop({"bybit_linear": C()}, rows.append, stop, 0.01)
    asyncio.run(go())
    assert rows[0]["ok"] and rows[0]["server_ns"] == 1793000000123456789 and rows[0]["t_ret_mono_ns"] - rows[0]["t_send_mono_ns"] == 4_000_000


def _write_gz(path: Path, recs):
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")


def test_derive_on_a_constructed_run(tmp_path):
    run = tmp_path / "hostmeasure_x"
    run.mkdir()
    (run / "hostmeasure_meta.json").write_text(json.dumps({"host": "h", "hours": 0.01}))
    ref_s = T0 // 10**9 - 30
    with (run / "clock.jsonl").open("w") as f:                                  # host 2 ms fast of NTP; one failed read; one unsynchronised
        f.write(json.dumps({"t_wall_ns": T0, "t_mono_ns": 1, "rc": 0, "text": chrony_text(0.002, True, ref_s), "stderr": ""}) + "\n")
        f.write(json.dumps({"t_wall_ns": T0 + 60 * 10**9, "t_mono_ns": 2, "rc": None, "text": "", "stderr": "FileNotFoundError"}) + "\n")
        f.write(json.dumps({"t_wall_ns": T0 + 120 * 10**9, "t_mono_ns": 3, "rc": 0, "text": chrony_text(0.0, True, 0).replace("Normal", "Not synchronised"), "stderr": ""}) + "\n")
    with (run / "rest_latency.jsonl").open("w") as f:
        f.write(json.dumps({"market": "binance_futures", "ok": True, "t_send_wall_ns": T0 + 10**9, "t_ret_wall_ns": T0 + 10**9 + 6_000_000, "t_send_mono_ns": 0, "t_ret_mono_ns": 6_000_000,
                            "server_ns": T0 + 10**9 + 3_000_000 - 2_000_000}) + "\n")         # venue clock = NTP; host 2 ms fast
    # bbo: Bybit moves first, Binance follows 20 ms later (local receive time), both 1 bps steps; venue event times 5 ms before receipt
    cap = run / "fillprobe_mainnet_x" / "capture"
    hour = (T0 // 10**9 // 3600) * 3600 * 1000
    by, bn = [], []
    mid = 100.0
    for k in range(50):
        t = T0 / 1e9 + 1 + k * 0.2
        step = 0.01 if k % 2 == 0 else -0.01
        mid += step
        by.append({"exchange_ts": t - 0.005, "local_ts": t, "bid": mid - 0.005, "ask": mid + 0.005})
        bn.append({"exchange_ts": t + 0.015, "local_ts": t + 0.02, "bid": mid - 0.005, "ask": mid + 0.005})
    date = "date=" + __import__("datetime").datetime.fromtimestamp(T0 / 1e9, __import__("datetime").timezone.utc).strftime("%Y-%m-%d")
    _write_gz(cap / "bybit" / "bbo" / "pair=SOLUSDT" / date / f"{hour}.jsonl.gz", by)
    _write_gz(cap / "binance" / "bbo" / "pair=SOLUSDT" / date / f"{hour}.jsonl.gz", bn)
    s = hm.derive(run, SRC, min_move_bps=0.5, window_ms=100.0)
    c = s["clock"]
    assert (c["records"], c["chronyc_failed"], c["unsynchronised"]) == (3, 1, 1)
    assert c["offset_ms"]["p50"] == 2.0 and c["uncertainty_ms"]["p50"] == 2.0 + 0.5 + 1.0          # |2| + 0.5 + 2/2
    assert c["last_update_age_s"]["p50"] == 30.0
    r = s["rest"]["binance_futures"]
    assert r["round_trip_ms"]["p50"] == 6.0 and r["venue_clock_minus_host_ms"]["p50"] == -2.0 and r["venue_clock_minus_ntp_ms"]["p50"] == 0.0
    ws = s["websocket_latency"]["bybit:bbo"]
    assert ws["receive_minus_event_ms"]["p50"] == 5.0 and ws["corrected_by_chrony_ms"]["p50"] == 3.0          # 5 ms raw, host 2 ms fast
    assert s["book_publication_interval"]["bybit:bbo"]["publication_interval_ms"]["p50"] == 200.0
    ll = s["lead_lag"]["SOLUSDT"]["on_local_receive_time"]
    assert ll["matched"] == 49 and ll["a_first"] == 49 and ll["lag_b_minus_a_ms"]["p50"] == 20.0         # the first bbo only sets the reference mid
    assert (run / "hostmeasure_summary.json").exists()
