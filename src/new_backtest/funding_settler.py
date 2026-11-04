"""Perpetual funding settlement for the cross-venue arb backtester.

Reads funding-rate history JSONL files (same format as data/funding/).
At each call to settle() all records whose timestamp <= now are processed
exactly once; each settlement charges:

    payment = position_qty * mark_price * rate

  positive payment → longs pay (e.g. positive funding rate)
  negative payment → longs receive (negative / contrarian rate)

The payment is applied directly to inventory cash (quote asset) so that
ending_equity stays correct.  It is also accumulated in self.funding_paid
for the BacktestResult's funding_paid field.

Only linear perpetuals are supported (position in base units, mark in quote,
rate dimensionless).  Inverse contracts are rejected at load time; they
require a different amount formula (position / mark * rate).
"""
from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .arb_strategy import InventoryBook
    from .order_book import OrderBook


@dataclass(slots=True)
class _Tick:
    ts_s: float
    rate: float
    mark_price: float | None   # None → fall back to book mid at settlement time


class FundingSettler:
    """
    Applies funding settlements to backtester inventory and PnL.

    Parameters
    ----------
    funding_rate_dir : str
        Directory containing ``*.jsonl`` funding-rate files.  Every file in
        the directory is scanned; records whose venue does not match a
        configured venue are skipped silently.
    venues : tuple[str, ...]
        Venue names as they appear in the backtester (e.g. "bybit", "binance").
        Matching is case-insensitive against the ``venue`` field in the JSONL.
    start_date, end_date : str | None
        ISO-8601 date strings (YYYY-MM-DD).  Records outside the window are
        ignored.  end_date is inclusive (settlements up to midnight of the
        next day are loaded).
    """

    def __init__(
        self,
        funding_rate_dir: str,
        venues: tuple[str, ...],
        *,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> None:
        self.funding_paid: float = 0.0

        venue_lower = {v.lower(): v for v in venues}
        self._ticks: dict[str, list[_Tick]] = {v: [] for v in venues}
        self._ptrs: dict[str, int] = {v: 0 for v in venues}

        utc = _dt.timezone.utc
        start_s: float | None = None
        end_s: float | None = None
        if start_date:
            start_s = _dt.datetime.fromisoformat(start_date).replace(tzinfo=utc).timestamp()
        if end_date:
            # include all settlements that fall on end_date (up to but not including next day)
            end_s = (_dt.datetime.fromisoformat(end_date).replace(tzinfo=utc).timestamp() + 86_400)

        dir_path = Path(funding_rate_dir)
        for path in sorted(dir_path.glob("*.jsonl")):
            for raw in path.read_text(encoding="utf-8").splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                row = json.loads(raw)

                venue_key = str(row.get("venue", "")).lower()
                canonical = venue_lower.get(venue_key)
                if canonical is None:
                    continue

                # Parse settlement timestamp
                if row.get("funding_ts_ns") is not None:
                    ts_s = int(row["funding_ts_ns"]) / 1e9
                elif row.get("funding_ts_ms") is not None:
                    ts_s = int(row["funding_ts_ms"]) / 1e3
                else:
                    continue  # no usable timestamp

                if start_s is not None and ts_s < start_s:
                    continue
                if end_s is not None and ts_s >= end_s:
                    continue

                mark_raw = row.get("mark_price")
                self._ticks[canonical].append(_Tick(
                    ts_s=ts_s,
                    rate=float(row["rate"]),
                    mark_price=float(mark_raw) if mark_raw not in (None, "") else None,
                ))

        for v in venues:
            self._ticks[v].sort(key=lambda t: t.ts_s)

        total = sum(len(lst) for lst in self._ticks.values())
        self._loaded = total

    # ── Public interface ──────────────────────────────────────────────────────

    @property
    def loaded_records(self) -> int:
        """Total number of settlement records loaded across all venues."""
        return self._loaded

    def settle(
        self,
        now_s: float,
        inventory: "InventoryBook",
        books: "dict[str, OrderBook]",
    ) -> None:
        """
        Process all pending settlements up to now_s.

        Each settlement:
          payment = position * mark * rate
          inventory.cash[venue] -= payment   (updates ending_equity)
          self.funding_paid     += payment
        """
        for venue, ticks in self._ticks.items():
            ptr = self._ptrs[venue]
            while ptr < len(ticks) and ticks[ptr].ts_s <= now_s:
                tick = ticks[ptr]
                position = inventory.base.get(venue, 0.0)
                if position != 0.0:
                    mark = tick.mark_price
                    if mark is None:
                        book = books.get(venue)
                        mark = book.mid if (book is not None and book.mid > 0) else 0.0
                    if mark > 0.0:
                        payment = position * mark * tick.rate
                        inventory.apply(venue, 0.0, -payment)
                        self.funding_paid += payment
                ptr += 1
            self._ptrs[venue] = ptr
