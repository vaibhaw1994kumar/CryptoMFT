"""W2b, Q3 dry run (a run wrapper, no logic; read-only on the recording): cut 90 s + 90 s around the three all-channel delivery stalls of the 8 Nov paper recording (d7d-run2), audit each window against
the recording it came from, and replay each through the session replay (v1_10, the profile's own safety values). EXPLORATORY: mechanics only; a window's seed books are constructed state (labelled)."""
import json
import sys
import time
from pathlib import Path

ROOT = Path("/Users/vaibhav/Documents/Newproject")
sys.path.insert(0, str(ROOT / "crypto_MFT_arb_platform/src"))
from crypto_stat_arb.live_feed import cut_recording_window
from crypto_stat_arb.session_audit import audit_incident
from new_backtest.profiles import apply_profile, load_profile
from new_backtest.session_replay import SessionReplay, dated_ticks, iter_recorded_events, profile_safety_default, write_result, write_run_record

SOURCE = ROOT / "out/integration/paper_live/run2/d7d-run2"
OUT = ROOT / "out/integration/w2b/q3_dry_run"
PROFILE = "perp_perp_maker_taker_v1_10"
STALLS = (("stall_1594ms", 1794177508750766000, "all-channel delivery stall, 1,594 ms"), ("stall_1941ms", 1794177630698040000, "all-channel delivery stall, 1,941 ms"),
          ("stall_1302ms", 1794179388847609000, "all-channel delivery stall, 1,302 ms; 36 s before the recording ends"))
summary = []
for name, trigger_ns, detail in STALLS:
    t = time.time()
    window = OUT / name
    manifest = cut_recording_window(SOURCE, window, trigger_ns, pre_seconds=90.0, post_seconds=90.0, rule="feed_unsafe", detail=detail)
    audit = audit_incident(window, OUT / f"{name}_audit", source_session_dir=SOURCE)
    profile = load_profile(PROFILE)
    cfg = apply_profile(profile, dataset="mar2024", data_root="", pair="SOLUSDT", start_date="2026-11-08", end_date="2026-11-08", output_dir=str(OUT / f"{name}_replay"))
    cfg.arb.max_position_base = 1.0
    replay = SessionReplay(cfg, profile, ticks=dated_ticks("2026-10-29"), recorded=True, mark_from_mid=True, profile_safety=profile_safety_default(profile))
    result = replay.run_recorded(iter_recorded_events(window))
    result["run"] = {"profile": PROFILE, "window": name, "labels": ["EXPLORATORY: mechanics only", "an incident window of the 8 Nov paper recording; the books start from labelled seed snapshots", "the session starts flat at the window start"]}
    write_result(result, OUT / f"{name}_replay")
    record = write_run_record(OUT / f"{name}_replay", replay)
    summary.append({"window": name, "detail": detail, "completeness": manifest["completeness"], "incomplete_reasons": manifest["incomplete_reasons"], "pre_s": manifest["window"]["pre_seconds"],
                    "post_s": manifest["window"]["post_seconds"], "frame_counts": manifest["frame_counts"], "seeds": {v: s["status"] for v, s in manifest["seeds"].items()},
                    "audit_ok": audit["ok"], "audit_checks": {k: c["ok"] for k, c in audit["checks"].items()}, "book_events_compared_with_the_recording": audit["checks"].get("source_books", {}).get("events_compared"),
                    "book_states_in_the_window": audit["checks"]["books"]["states_by_venue"],
                    "replay": {"events": result["events"], "signals": len(result["signals"]), "orders": len(result["orders"]), "fills": len(result["fills"]), "ledger_events": sum(result["ledger_event_counts"].values()),
                               "data_span_s": record["data_span_s"], "speed_x": record["speed_x"], "input_files": record["input_files"]},
                    "wall_s": round(time.time() - t, 1)})
    print(json.dumps(summary[-1], indent=1), flush=True)
(OUT / "q3_dry_run_summary.json").write_text(json.dumps({"label": "EXPLORATORY: Q3 dry run of the incident tooling on the 8 Nov paper recording (read-only on it)", "source": str(SOURCE), "profile": PROFILE, "windows": summary}, indent=1))
