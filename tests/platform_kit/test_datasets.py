import json

from trading_platform_poc.datasets import list_datasets


def test_lists_manifest_datasets_and_ignores_unrelated_directories(
    tmp_path,
) -> None:
    dataset = tmp_path / "run-1"
    dataset.mkdir()
    (dataset / "dataset_manifest.json").write_text(
        json.dumps(
            {
                "event_count": 42,
                "symbol": "BTCUSDT",
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "unrelated").mkdir()

    assert list_datasets(tmp_path) == [
        {
            "dataset_id": "run-1",
            "path": str(dataset),
            "event_count": 42,
            "symbol": "BTCUSDT",
        }
    ]
