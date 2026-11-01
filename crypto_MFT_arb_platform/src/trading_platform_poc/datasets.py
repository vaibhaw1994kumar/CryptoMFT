from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def list_datasets(data_directory: Path) -> list[dict[str, Any]]:
    if not data_directory.is_dir():
        return []
    datasets: list[dict[str, Any]] = []
    for directory in data_directory.iterdir():
        if not directory.is_dir():
            continue
        manifest_path = directory / "dataset_manifest.json"
        raw_path = directory / "raw.jsonl"
        parquet_path = directory / "events.parquet"
        if not manifest_path.is_file() and not (
            raw_path.is_file() or parquet_path.is_file()
        ):
            continue
        manifest: dict[str, Any] = {}
        if manifest_path.is_file():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        datasets.append(
            {
                "dataset_id": directory.name,
                "path": str(directory),
                "event_count": manifest.get("event_count"),
                "symbol": manifest.get("symbol"),
            }
        )
    return sorted(datasets, key=lambda item: item["dataset_id"])
