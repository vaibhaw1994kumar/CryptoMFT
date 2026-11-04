"""Queue model v2, step 1: the run-time side of the data manifest (scripts/queue_v2_manifest.py writes it).

A day whose gzip stream is truncated is blocked unless the run carries `allow_rejected_data` with a written reason; the reason and the
manifest hash are recorded in every result. Opt-in: ArbBacktestConfig.data_manifest = None keeps every existing run unchanged."""
from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from pathlib import Path


class DataManifestError(RuntimeError):
    pass


def _days(a: str, b: str):
    d0, d1 = date.fromisoformat(a), date.fromisoformat(b)
    return [(d0 + timedelta(n)).isoformat() for n in range((d1 - d0).days + 1)]


def gate(path: str, venues: tuple[str, ...], pair: str, start_day: str, end_day: str, allow_reason: str | None,
         skipped_snapshot_venues: tuple[str, ...] = ()) -> dict:
    raw = Path(path).read_bytes()
    man = json.loads(raw)
    rows, blocked = [], []
    for v in venues:
        for day in _days(start_day, end_day):
            e = man["days"].get(f"{v}/{pair}/{day}")
            if e is None:
                rows.append({"venue": v, "day": day, "status": "NOT_IN_MANIFEST"})
                blocked.append((v, day, "not in manifest"))
                continue
            if e.get("book_events_is_trade_copy"):
                blocked.append((v, day, "book_events are a copy of the trade tape (no depth data): this venue has no usable book that day"))
            if e["channels"].get("book_snapshots", {}).get("placeholder") and v not in skipped_snapshot_venues:
                blocked.append((v, day, "placeholder book snapshots (every level the same size): pass skip_snapshot_venues or a written reason"))
            trunc = [c for c, x in e["channels"].items() if x["truncated"]]
            empty = [c for c, x in e["channels"].items() if x["rows"] == 0]
            rows.append({"venue": v, "day": day, "status": e["status"], "placeholder_snapshots": bool(e["channels"].get("book_snapshots", {}).get("placeholder")),
                         "truncated_channels": trunc, "empty_channels": empty,
                         "rows": {c: x["rows"] for c, x in e["channels"].items()}})
            if trunc:
                blocked.append((v, day, "truncated gzip in " + ",".join(trunc)))
    flags = []                                    # rebuild acceptance (step 2): a day under 99 % touch match is flagged in every result that uses it
    acc_dir = Path(path).parent / "rebuild_acceptance"
    for v in venues:
        for day in _days(start_day, end_day):
            f = acc_dir / f"{v}_{day}.json"
            if f.exists():
                a = json.loads(f.read_text())
                if a.get("flag_below_99"):
                    eng = a["variants"]["engine_tape_repair_on"]
                    flags.append({"venue": v, "day": day, "touch_price_match_pct": eng["touch_price_match_pct"], "touch_size_match_pct": eng["touch_size_match_pct"],
                                  "note": a.get("flag_note", "")})
    unrec = sorted({f"{f['venue_dir']}/{f['symbol']}/{f['date']}" for f in man.get("files", []) if f.get("provenance_unrecorded")
                    and any(f["venue_dir"] == v and f["symbol"] == pair and start_day <= f["date"] <= end_day for v in venues)})
    doc = {"manifest": str(path), "manifest_sha256": hashlib.sha256(raw).hexdigest(), "days": rows,
           "truncated_days": [f"{v}/{d}" for v, d, why in blocked if why.startswith("truncated")], "override_reason": None,
           "placeholder_snapshot_days": [f"{r['venue']}/{r['day']}" for r in rows if r.get("placeholder_snapshots")],
           "snapshots_ignored_for": list(skipped_snapshot_venues),
           "rebuild_acceptance_flags": flags,
           "provenance_unrecorded_venue_days": unrec,           # capture region / client version not recorded in the files: listed, never blocking
           "provenance_note": "capture_region and client_version are declared per dataset and mostly UNKNOWN (see the manifest)"}
    hard = [(v, d, why) for v, d, why in blocked if why.startswith("book_events are a copy")]
    if hard:                                                  # no written reason can turn a trade tape into a book
        raise DataManifestError("data manifest blocks the run (not overridable): " + "; ".join(f"{v} {d}: {why}" for v, d, why in hard))
    if blocked:
        if not allow_reason or not allow_reason.strip():
            raise DataManifestError("data manifest blocks the run: " + "; ".join(f"{v} {d}: {why}" for v, d, why in blocked)
                                    + " (pass allow_rejected_data with a written reason to run anyway)")
        doc["override_reason"] = allow_reason.strip()
    return doc
