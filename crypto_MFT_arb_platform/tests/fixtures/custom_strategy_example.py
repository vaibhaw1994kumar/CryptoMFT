from hft_platform_kit import BinanceOrderBook, Order, OrderIntent, create_engine


class NoOpStrategy:
    strategy_id = "no_op"
    strategy_version = "1.0.0"

    def evaluate(
        self,
        book: BinanceOrderBook,
        timestamp_ns: int,
        open_orders: tuple[Order, ...],
    ) -> tuple[OrderIntent, ...]:
        return ()

    def should_cancel(self, book: BinanceOrderBook) -> bool:
        return False


engine = create_engine(strategy=NoOpStrategy())
print(type(engine).__name__)
