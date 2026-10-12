import asyncio
import sys

from hft_platform_kit import replay_dataset


async def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python examples/sdk_replay.py DATASET_PATH")
    engine = await replay_dataset(sys.argv[1])
    print("book_checksum:", engine.book.checksum())
    print("portfolio:", engine.portfolio.snapshot(engine.book.mid_price))


if __name__ == "__main__":
    asyncio.run(main())
