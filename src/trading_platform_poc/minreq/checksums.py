"""Venue-published order-book checksums (MR-02B book integrity).

OKX stamps every ``books`` message with a CRC32 over the top 25 levels of the
book *after* the message is applied. Verifying it is the only way to prove the
local book matches the venue's, because a book can be sequence-valid and still
wrong when a level update is lost inside one message.

Binance spot depth carries no checksum and Bybit spot ``orderbook.50`` carries
none either, so for those venues :func:`verify_book_checksum` returns ``None``
(not verifiable) rather than pretending a check happened.
"""

from __future__ import annotations

import heapq
import zlib
from decimal import Decimal
from typing import Iterable, Mapping

OKX_CHECKSUM_DEPTH = 25


def _signed_crc32(text: str) -> int:
    value = zlib.crc32(text.encode("utf-8")) & 0xFFFFFFFF
    return value - (1 << 32) if value >= (1 << 31) else value


def okx_checksum_from_levels(
    bids: Iterable[tuple[Decimal, Decimal]],
    asks: Iterable[tuple[Decimal, Decimal]],
) -> int:
    """CRC32 exactly as OKX defines it: bid and ask levels interleaved.

    ``bids`` must be best-first (descending) and ``asks`` best-first
    (ascending). Prices and sizes are rendered with ``str`` so the digits the
    venue sent are the digits that are hashed.
    """

    bid_rows = list(bids)[:OKX_CHECKSUM_DEPTH]
    ask_rows = list(asks)[:OKX_CHECKSUM_DEPTH]
    parts: list[str] = []
    for index in range(OKX_CHECKSUM_DEPTH):
        if index < len(bid_rows):
            price, size = bid_rows[index]
            parts.append(f"{price}:{size}")
        if index < len(ask_rows):
            price, size = ask_rows[index]
            parts.append(f"{price}:{size}")
    return _signed_crc32(":".join(parts))


def okx_book_checksum(
    bids: Mapping[Decimal, Decimal], asks: Mapping[Decimal, Decimal]
) -> int:
    """Checksum of a full price -> size book held as two dictionaries."""

    top_bids = heapq.nlargest(OKX_CHECKSUM_DEPTH, bids.items())
    top_asks = heapq.nsmallest(OKX_CHECKSUM_DEPTH, asks.items())
    return okx_checksum_from_levels(top_bids, top_asks)


def verify_book_checksum(
    venue: str,
    bids: Mapping[Decimal, Decimal],
    asks: Mapping[Decimal, Decimal],
    expected: int,
) -> bool | None:
    """True/False when the venue publishes a checksum, None when it does not."""

    if venue.lower() != "okx":
        return None
    return okx_book_checksum(bids, asks) == int(expected)
