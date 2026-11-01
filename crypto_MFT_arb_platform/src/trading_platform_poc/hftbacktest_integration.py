from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from trading_platform_poc.optional_dependencies import (
    require_hftbacktest,
    require_numpy,
    require_pyarrow_parquet,
)

# Two different event logs live in this repository and only one of them is
# accepted here. Saying so costs a sentence and saves the reader from a
# KeyError naming a field they never heard of.
_EXPECTED_INPUT = (
    "Expected a normalized event log written by `record` or `tardis-import`: "
    "a directory holding events.jsonl or events.parquet, or one of those "
    "files. The canonical log written by `mr-generate` uses the separate "
    "minimum-requirements schema and is not interchangeable with it."
)


def export_hftbacktest_npz(data_path: Path, output_path: Path) -> int:
    hftbacktest = require_hftbacktest()
    np = require_numpy()
    rows: list[tuple[int, int, int, float, float, int, int, float]] = []
    for position, event in enumerate(_read_normalized_events(data_path), start=1):
        try:
            _append_event_rows(rows, event, hftbacktest)
        except KeyError as error:
            raise ValueError(
                f"event {position} of {data_path} is missing {error}. "
                f"{_EXPECTED_INPUT}"
            ) from error
        except TypeError as error:
            raise ValueError(
                f"event {position} of {data_path} is malformed: {error}. "
                f"{_EXPECTED_INPUT}"
            ) from error

    data = np.array(rows, dtype=hftbacktest.event_dtype)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as output:
        np.savez_compressed(output, data=data)
    return len(data)


def _append_event_rows(
    rows: list[tuple[int, int, int, float, float, int, int, float]],
    event: dict[str, Any],
    hftbacktest: Any,
) -> None:
    payload = event["payload"]
    event_type = event["event_type"]
    exchange_timestamp_ns = int(event["exchange_timestamp_ns"])
    receive_timestamp_ns = int(event["receive_timestamp_ns"])
    base_flags = hftbacktest.EXCH_EVENT | hftbacktest.LOCAL_EVENT
    if event_type in {"book_snapshot", "book_delta"}:
        depth_flag = (
            hftbacktest.DEPTH_SNAPSHOT_EVENT
            if event_type == "book_snapshot"
            else hftbacktest.DEPTH_EVENT
        )
        for side_name, side_flag in (
            ("bids", hftbacktest.BUY_EVENT),
            ("asks", hftbacktest.SELL_EVENT),
        ):
            for level in payload[side_name]:
                rows.append(
                    (
                        base_flags | depth_flag | side_flag,
                        exchange_timestamp_ns,
                        receive_timestamp_ns,
                        float(level["price"]),
                        float(level["quantity"]),
                        0,
                        0,
                        0.0,
                    )
                )
    elif event_type == "trade":
        side_flag = (
            hftbacktest.SELL_EVENT
            if payload["buyer_is_maker"]
            else hftbacktest.BUY_EVENT
        )
        rows.append(
            (
                base_flags | hftbacktest.TRADE_EVENT | side_flag,
                exchange_timestamp_ns,
                receive_timestamp_ns,
                float(payload["price"]),
                float(payload["quantity"]),
                0,
                0,
                0.0,
            )
        )


def hftbacktest_health() -> dict[str, Any]:
    hftbacktest = require_hftbacktest()
    np = require_numpy()
    probe = np.empty(0, dtype=hftbacktest.event_dtype)
    asset = hftbacktest.BacktestAsset().data(probe)
    return {
        "version": hftbacktest.__version__,
        "event_dtype": list(hftbacktest.event_dtype.names or ()),
        "asset_type": type(asset).__name__,
    }


def _read_normalized_events(data_path: Path):
    path = data_path
    if path.is_dir():
        parquet_path = path / "events.parquet"
        jsonl_path = path / "events.jsonl"
        path = parquet_path if parquet_path.is_file() else jsonl_path
    if not path.is_file():
        raise FileNotFoundError(f"normalized dataset does not exist: {path}")
    if path.suffix == ".parquet":
        pq = require_pyarrow_parquet()
        for row in pq.read_table(path).to_pylist():
            row["payload"] = json.loads(row.pop("payload_json"))
            yield row
        return
    # NPZ is what this module writes, so being handed one almost always means
    # --data and --output were swapped. Naming that is more use than reporting
    # the first byte that failed to decode.
    if path.suffix == ".npz":
        raise ValueError(
            f"{path} is HftBacktest NPZ, which is the output of this command "
            f"rather than its input. {_EXPECTED_INPUT}"
        )
    try:
        with path.open(encoding="utf-8") as source:
            for number, line in enumerate(source, start=1):
                if not line.strip():
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as error:
                    raise ValueError(
                        f"{path} line {number} is not JSON. {_EXPECTED_INPUT}"
                    ) from error
    except UnicodeDecodeError as error:
        raise ValueError(
            f"{path} is binary, not a text event log. {_EXPECTED_INPUT}"
        ) from error
