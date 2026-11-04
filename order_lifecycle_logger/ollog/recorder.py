from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from .endpoints import LABEL


def utc_tag() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


class Recorder:
    """One directory per market: events.jsonl (everything, local only), orders.jsonl (one record per order), meta.json."""

    def __init__(self, root: Path, market: str):
        self.dir = Path(root) / f"{market}_{utc_tag()}"
        self.dir.mkdir(parents=True, exist_ok=False)
        self._events = (self.dir / "events.jsonl").open("a", encoding="utf-8")
        self._orders = (self.dir / "orders.jsonl").open("a", encoding="utf-8")

    @staticmethod
    def _line(fh, obj) -> None:
        fh.write(json.dumps(obj, sort_keys=True, default=str) + "\n")
        fh.flush()

    def event(self, etype: str, /, **fields) -> None:
        self._line(self._events, {"label": LABEL, "type": etype, **fields})

    def order(self, record: dict) -> None:
        self._line(self._orders, {"label": LABEL, **record})

    def meta(self, obj: dict) -> None:
        (self.dir / "meta.json").write_text(json.dumps({"label": LABEL, **obj}, indent=2, sort_keys=True, default=str))

    def close(self) -> None:
        for fh in (self._events, self._orders):
            try:
                fh.close()
            except Exception:
                pass
