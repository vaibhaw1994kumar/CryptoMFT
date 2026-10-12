import hft_platform_kit._ems as ems


quote = ems.VenueQuote()
quote.venue = "binance"
quote.symbol = "BTCUSDT"
quote.bid = 99.0
quote.ask = 100.0
quote.bid_size = 2.0
quote.ask_size = 2.0
quote.fee_bps = 0.0
quote.latency_us = 0.0
quote.healthy = True

slices = ems.Scheduler.twap(10, 20, 1.0, 4)
routes = ems.SmartOrderRouter().route(ems.Side.BUY, 1.0, [quote])

print("twap:", [(item.timestamp_ns, item.quantity) for item in slices])
print("route:", [(item.venue, item.quantity) for item in routes])
