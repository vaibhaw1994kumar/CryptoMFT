"""Rebuild a live part's ``raw.jsonl`` from its ``wire.jsonl``.

``raw.jsonl`` is derived deterministically from the pre-parse wire frames:
each successfully parsed JSON-object frame is appended, in wire order, through
the same ``RawCaptureWriter`` the live recorder uses (record ids and the hash
chain depend only on the part's session id, ordinal and payload). A rebuilt
file is accepted only if its SHA-256 and record count equal the values the
live recorder wrote to ``raw_manifest.json``; otherwise it is discarded.

    # prove the method on parts that still have raw.jsonl (nothing is written)
    python3.12 scripts/rebuild_raw_from_wire.py --verify-intact runs/paper_watch/live
    # restore missing raw.jsonl files (only checksum-verified rebuilds are kept)
    python3.12 scripts/rebuild_raw_from_wire.py --restore runs/paper_watch/live
"""

from __future__ import annotations

import argparse
import base64
from hashlib import sha256
import json
from pathlib import Path
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from crypto_stat_arb.raw_capture import RawCaptureWriter  # noqa: E402


def adapter_version(venue: str, channel: str) -> str:
    """The adapter version the live feed stamps for each wire channel."""
    if channel == "rest-depth-snapshot":
        return "binance-rest-depth-v1"
    if channel == "trade" or channel.startswith("publicTrade."):
        return "binance-trade-v1" if venue == "BINANCE" else "bybit-public-trade-v1"
    return "binance-depth-v1" if venue == "BINANCE" else "bybit-depth-v1"


def file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def rebuild(part: Path, scratch: Path) -> tuple[Path, int]:
    manifest = json.loads((part / "raw_manifest.json").read_text())
    writer = RawCaptureWriter(scratch, manifest["session_id"], durable=False)
    count = 0
    with (part / "wire.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            wire = json.loads(line)
            if wire["frame_encoding"] == "BASE64":
                original: str | bytes = base64.b64decode(wire["frame_data"])
                text = original.decode("utf-8", errors="strict")
            else:
                original = text = wire["frame_data"]
            try:
                payload = json.loads(text)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue  # live: PARSE rejection, no raw record
            if not isinstance(payload, dict):
                continue
            writer.append(
                venue=wire["venue"], vendor=wire["venue"], channel=wire["channel"],
                host=wire["host"], receive_ts_ns=int(wire["receive_ts_ns"]), payload=payload,
                adapter_version=adapter_version(wire["venue"], wire["channel"]),
                original_frame=original, transport=wire["transport"], wire_ref=wire["wire_ref"],
            )
            count += 1
    writer.close()
    return writer.raw_path, count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--verify-intact", action="store_true",
                      help="rebuild parts that still have raw.jsonl and compare (writes nothing)")
    mode.add_argument("--restore", action="store_true",
                      help="rebuild missing raw.jsonl and keep only checksum-verified files")
    parser.add_argument("root", type=Path, help="directory containing cycle-*/part-*")
    parser.add_argument("--limit", type=int, default=0, help="max parts to process (0 = all)")
    options = parser.parse_args()

    parts = sorted(options.root.glob("cycle-*/part-*"))
    if options.verify_intact:
        parts = [p for p in parts if (p / "raw.jsonl").exists() and (p / "wire.jsonl").exists()]
    else:
        parts = [p for p in parts if not (p / "raw.jsonl").exists()]
    if options.limit:
        parts = parts[: options.limit]
    results = []
    for part in parts:
        name = "/".join(part.parts[-2:])
        manifest_path = part / "raw_manifest.json"
        if not (part / "wire.jsonl").exists() or not manifest_path.exists():
            results.append({"part": name, "status": "UNRECOVERABLE_NO_WIRE"})
            print(f"{name}: UNRECOVERABLE (no wire.jsonl)", flush=True)
            continue
        expected = json.loads(manifest_path.read_text())
        with tempfile.TemporaryDirectory() as scratch:
            rebuilt, count = rebuild(part, Path(scratch))
            digest = file_sha256(rebuilt)
            matches = digest == expected["raw_file_sha256"] and count == expected["record_count"]
            if options.verify_intact:
                identical = digest == file_sha256(part / "raw.jsonl")
                status = "IDENTICAL" if identical and matches else "MISMATCH"
            elif matches:
                target = part / "raw.jsonl"
                if target.exists():
                    status = "SKIPPED_EXISTS"
                else:
                    shutil.move(str(rebuilt), str(target))
                    status = "RESTORED"
            else:
                status = "REJECTED_CHECKSUM_MISMATCH"
        results.append({"part": name, "status": status, "records": count,
                        "expected_records": expected["record_count"], "sha256": digest,
                        "expected_sha256": expected["raw_file_sha256"]})
        print(f"{name}: {status} records={count}/{expected['record_count']} sha={digest[:16]}", flush=True)
    print(json.dumps({
        "mode": "verify-intact" if options.verify_intact else "restore",
        "counts": {s: sum(r["status"] == s for r in results) for s in sorted({r["status"] for r in results})},
    }))


if __name__ == "__main__":
    main()
